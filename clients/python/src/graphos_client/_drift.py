"""Readable lines for a registration's drift report.

A registration's dry run (``POST /v1/tenants?dry_run=true``) answers ``drift``
(ADR-0013, ADR-0014): how the tenant differs from what the body declares.
Its parts:

- ``not_declared``: what the tenant has that the body does not mention
  (added elsewhere, typically in the web UI).  A merge keeps it.
- ``removes``: what a replace (``force_shrink``) would take away.
- ``overwrites``: entries the body would replace with a different value.
- ``settings``: settings the body would change, ``{name: [before, after]}``
  (``strict_endpoints``, ``property_mode``, ``community_algorithm``).
- ``recreates``: labels, edge types and label pairs the body declares that
  were dropped or removed earlier; registering creates them again.

``not_declared``, ``removes`` and ``overwrites`` are ``{part: items}``, named as
the change feed names them: ``vertex_labels``, ``edge_labels``,
``search_indexes``, ``meta`` (keys), ``resolve_rules`` (edge types) by name;
``edge_endpoints`` as ``[type, src, dst]``; ``label_hierarchy`` as ``[child,
parent]``; ``property_indexes`` and ``generated_columns`` as description
strings; the by-entry parts (``display``, ``property_schema``, ``deprecated``) as
``{"vertex": [names], "edge": [names]}``.  ``recreates`` is ``{"vertex_labels":
[{"name", "removed_at", "removed_by"}], "edge_labels": [...], "edge_endpoints":
[{"pair": [type, src, dst], "removed_at", "removed_by"}]}``.

The server leaves out empty parts; a missing or null part, list or field
reads as empty.  A part this module does not know (a newer server's) is still
reported, under its own name.  :func:`drift_lines` turns the report into one
line per item, as :meth:`GraphosClient.ensure_tenant` logs them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, NamedTuple

_NOT_DECLARED = "is in the tenant but not declared here (added elsewhere, e.g. in the web UI)"
_REMOVED = "would be removed: the registration replaces the schema and does not declare it"
_REPLACED = "would be replaced: it differs from the stored one"

# The sections of a report that ``on_drift="fail"`` fails on: registering
# would undo, change or bring back what was done elsewhere.
BLOCKING = ("removes", "overwrites", "settings", "recreates")
_SECTIONS = ("not_declared", *BLOCKING)

# What one item of each part is called in a line.
_THINGS = {
    "vertex_labels": "vertex label",
    "edge_labels": "edge type",
    "edge_endpoints": "label pair",
    "label_hierarchy": "label hierarchy",
    "search_indexes": "search index",
    "resolve_rules": "resolve rule of edge type",
    "property_indexes": "property index",
    "generated_columns": "generated column",
    "meta": "meta key",
    "display": "display",
    "property_schema": "property schema",
    "deprecated": "deprecation",
}
_SIDES = {"vertex": "vertex label", "edge": "edge type"}
_SETTINGS = {
    "strict_endpoints": "strict_endpoints",
    "property_mode": "the property mode",
    "community_algorithm": "the community algorithm",
}


class DriftLines(NamedTuple):
    """The lines of a drift report, by how they are logged."""

    info: list[str]          # ``not_declared``: a merge keeps it
    warning: list[str]       # everything else
    blocking: list[str]      # the ``warning`` lines ``on_drift="fail"`` fails on


def _items(value: Any) -> list[Any]:
    """A part's items: a null or missing part reads as empty."""
    if value is None:
        return []
    if isinstance(value, (str, Mapping)):
        return [value]
    return list(value)


def _thing(part: str) -> str:
    return _THINGS.get(part, part.replace("_", " "))


def _pair(value: Any) -> str:
    """``Team-[OWNS]->Doc`` from ``["OWNS", "Team", "Doc"]`` or a mapping."""
    if isinstance(value, Mapping):
        edge_type = value.get("type") or value.get("name")
        src = value.get("src") or value.get("src_label")
        dst = value.get("dst") or value.get("dst_label")
    else:
        edge_type, src, dst = ([*_items(value), None, None, None])[:3]
    return f"{src}-[{edge_type}]->{dst}"


def _item(part: str, item: Any) -> str:
    """One item of ``part`` as a line names it."""
    if part == "edge_endpoints":
        return f"{_thing(part)} {_pair(item)}"
    if part == "label_hierarchy" and not isinstance(item, (str, Mapping)):
        child, parent = ([*_items(item), None, None])[:2]
        return f"{_thing(part)} '{child}' under '{parent}'"
    if isinstance(item, (list, tuple)):
        item = ", ".join(str(x) for x in item)
    return f"{_thing(part)} '{item}'"


def _lines(tenant: str, section: Mapping[str, Any], suffix: str) -> Iterable[str]:
    """One line per item of a ``{part: items}`` section; the by-entry parts
    (and any part given as a mapping) per vertex and edge."""
    for part, value in section.items():
        if isinstance(value, Mapping):
            for side, names in value.items():
                kind = _SIDES.get(side, side)
                for name in _items(names):
                    yield f"{tenant}: {_thing(part)} of {kind} '{name}' {suffix}"
        else:
            for item in _items(value):
                yield f"{tenant}: {_item(part, item)} {suffix}"


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None:
        return "none"
    return f"'{value}'"


def _settings(tenant: str, section: Mapping[str, Any]) -> Iterable[str]:
    for name, change in section.items():
        what = _SETTINGS.get(name, name.replace("_", " "))
        values = _items(change)
        if len(values) == 2:  # noqa: PLR2004 — [before, after]
            before, after = values
            yield f"{tenant}: {what} would change from {_value(before)} to {_value(after)}"
        else:
            yield f"{tenant}: {what} would change ({change!r})"


def _recreates(tenant: str, section: Mapping[str, Any]) -> Iterable[str]:
    for part, entries in section.items():
        for entry in _items(entries):
            fields: Mapping[str, Any] = entry if isinstance(entry, Mapping) else {"name": entry}
            when = f" on {fields['removed_at']}" if fields.get("removed_at") else ""
            who = f" by {fields['removed_by']}" if fields.get("removed_by") else ""
            if part == "edge_endpoints":
                pair = _pair(fields.get("pair") or fields)
                yield (f"{tenant}: label pair {pair} was removed{when}{who}; "
                       "registering declares it again")
            else:
                what = _item(part, fields.get("name"))
                yield f"{tenant}: {what} was dropped{when}{who}; registering creates it again"


def drift_lines(tenant: str, drift: Mapping[str, Any] | None) -> DriftLines:
    """One line per drift item, in the report's order, split by how it is logged.

    ``info`` holds the ``not_declared`` lines (a merge keeps those items, and
    most programs declare less than the tenant has); ``warning`` every other
    line; ``blocking`` the ``warning`` lines of ``removes``, ``overwrites``,
    ``settings`` and ``recreates``, which ``on_drift="fail"`` fails on.  A
    section the client does not know is logged as a warning, generically,
    and does not block.
    """
    report = drift or {}

    def section(name: str, lines: Callable[[str, Mapping[str, Any]], Iterable[str]]) -> list[str]:
        value = report.get(name)
        if not value:
            return []
        if not isinstance(value, Mapping):          # not the shape this client knows
            return [_generic(tenant, name, value)]
        return list(lines(tenant, value))

    info = section("not_declared", lambda t, v: _lines(t, v, _NOT_DECLARED))
    blocking = [
        *section("removes", lambda t, v: _lines(t, v, _REMOVED)),
        *section("overwrites", lambda t, v: _lines(t, v, _REPLACED)),
        *section("settings", _settings),
        *section("recreates", _recreates),
    ]
    unknown = [_generic(tenant, name, value)
               for name, value in report.items() if name not in _SECTIONS and value]
    return DriftLines(info, blocking + unknown, blocking)


def _generic(tenant: str, section: str, value: Any) -> str:
    return f"{tenant}: drift {section.replace('_', ' ')}: {value!r}"

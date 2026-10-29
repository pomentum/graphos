"""Readable lines for a registration's drift report.

A registration's dry run (``POST /v1/tenants?dry_run=true``) answers ``drift``:
how the tenant differs from what the body declares.  Its three parts:

- ``not_declared``: what the tenant has that the body does not mention
  (added elsewhere, typically in the web UI).  Registering keeps it.
- ``overwrites``: entries the body would replace with a different value.
- ``recreates``: labels, edge types and label pairs the body declares that
  were dropped or removed earlier; registering creates them again.

The server may leave out empty lists and parts; a missing key reads as empty.
:func:`drift_lines` turns the report into one line per item, as
:meth:`GraphosClient.ensure_tenant` logs them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

_NOT_DECLARED = "is in the tenant but not declared here (added elsewhere, e.g. in the web UI)"
_REPLACED = "would be replaced: it differs from the stored one"

# What one item of each part of the report is called in a line.  A part the
# server adds later is still reported, under its own name.
_THINGS = {
    "vertex_labels": "vertex label",
    "edge_labels": "edge type",
    "edge_endpoints": "label pair",
    "search_indexes": "search index",
    "meta": "meta key",
    "display": "display",
    "property_schema": "property schema",
    "deprecated": "deprecation",
}
_SIDES = {"vertex": "vertex label", "edge": "edge type"}


def _part(report: Mapping[str, Any] | None, key: str) -> Any:
    return (report or {}).get(key) or ()


def _pair(value: Any) -> str:
    """``Team-[OWNS]->Doc`` from ``["OWNS", "Team", "Doc"]`` or a mapping."""
    if isinstance(value, Mapping):
        edge_type = value.get("type") or value.get("name")
        src = value.get("src") or value.get("src_label")
        dst = value.get("dst") or value.get("dst_label")
    else:
        edge_type, src, dst = (list(value) + [None, None, None])[:3]
    return f"{src}-[{edge_type}]->{dst}"


def _lines(tenant: str, report: Mapping[str, Any] | None, suffix: str) -> Iterable[str]:
    """One line per item of ``not_declared`` or ``overwrites``.

    A part is a list of names (pairs for ``edge_endpoints``) or, for the
    per-entry parts, ``{"vertex": [names], "edge": [names]}``.
    """
    for key, value in (report or {}).items():
        thing = _THINGS.get(key, key.replace("_", " "))
        if isinstance(value, Mapping):
            for side, names in value.items():
                kind = _SIDES.get(side, side)
                for name in names or ():
                    yield f"{tenant}: {thing} of {kind} '{name}' {suffix}"
        elif key == "edge_endpoints":
            for pair in value or ():
                yield f"{tenant}: {thing} {_pair(pair)} {suffix}"
        else:
            for name in value or ():
                yield f"{tenant}: {thing} '{name}' {suffix}"


def _recreate(tenant: str, item: Mapping[str, Any]) -> str:
    kind, name = item.get("kind"), item.get("name")
    when = f" on {item['removed_at']}" if item.get("removed_at") else ""
    who = f" by {item['removed_by']}" if item.get("removed_by") else ""
    if kind == "edge_endpoint":
        pair = _pair({"type": name, "src": item.get("src_label"), "dst": item.get("dst_label")})
        return (f"{tenant}: label pair {pair} was removed{when}{who}; "
                "registering declares it again")
    what = {"vertex_label": "vertex label", "edge_type": "edge type"}.get(str(kind), str(kind))
    return f"{tenant}: {what} '{name}' was dropped{when}{who}; registering creates it again"


def drift_lines(tenant: str, drift: Mapping[str, Any] | None) -> tuple[list[str], list[str]]:
    """One line per drift item, and those of them that ``on_drift="fail"`` fails on.

    The second list holds the ``overwrites`` and ``recreates`` lines:
    registering would undo or bring back a change made elsewhere.
    ``not_declared`` items alone never fail a registration, since registering
    keeps them.
    """
    if not drift:
        return [], []
    kept = list(_lines(tenant, drift.get("not_declared"), _NOT_DECLARED))
    blocking = list(_lines(tenant, drift.get("overwrites"), _REPLACED))
    blocking += [_recreate(tenant, item) for item in _part(drift, "recreates")
                 if isinstance(item, Mapping)]
    return kept + blocking, blocking

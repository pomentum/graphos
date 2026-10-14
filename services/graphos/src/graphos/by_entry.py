"""Spec keys kept per vertex label and per edge type, merged entry by entry.

``display`` and ``property_schema`` are shaped ``{"vertex": {name: entry},
"edge": {name: entry}}``. A merge registration replaces the entries it
declares, removes those it sends as ``null`` and keeps the rest; the same
rule is applied in Python (dry runs) and in SQL (``bootstrap.persist_spec``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

PARTS = ("vertex", "edge")

Entries = dict[str, dict[str, Any]]
Removed = tuple[tuple[str, str], ...]


def split(request: Any, dump: Callable[[Any], dict[str, Any]]) -> tuple[Entries, Removed]:
    """The entries a request model declares (each dumped with ``dump``) and the
    ``(part, name)`` pairs it removes."""
    declared: Entries = {}
    removed: list[tuple[str, str]] = []
    for part in PARTS:
        for name, entry in getattr(request, part).items():
            if entry is None:
                removed.append((part, name))
            else:
                declared.setdefault(part, {})[name] = dump(entry)
    return declared, tuple(removed)


def merge(stored: Mapping[str, Any], incoming: Mapping[str, Any], removed: Removed) -> Entries:
    """``stored`` with ``incoming``'s entries replacing its own and ``removed`` taken out."""
    gone = set(removed)
    out: Entries = {}
    for part in PARTS:
        entries = {
            name: entry
            for name, entry in {**(stored.get(part) or {}), **(incoming.get(part) or {})}.items()
            if (part, name) not in gone
        }
        if entries:
            out[part] = entries
    return out


def parse(raw: object, clean: Callable[[dict[str, Any]], dict[str, Any]] = dict) -> Entries:
    """The parts of a stored value, entries passed through ``clean``; empty parts left out."""
    data = raw if isinstance(raw, dict) else {}
    out: Entries = {}
    for part in PARTS:
        entries = data.get(part)
        if isinstance(entries, dict):
            kept = {str(name): clean(e) for name, e in entries.items() if isinstance(e, dict)}
            if kept:
                out[part] = kept
    return out


def names(removed: Removed, part: str) -> list[str]:
    return [name for p, name in removed if p == part]


def sql(key: str, vertex_param: str, edge_param: str) -> str:
    """The ``jsonb_build_object`` arguments that merge ``key``'s parts in
    ``persist_spec`` (stored row: the target table; incoming: ``EXCLUDED``)."""
    def part(name: str, param: str) -> str:
        return (
            f"'{name}', NULLIF("
            f"(COALESCE(graphos_meta.tenants.spec #> '{{{key},{name}}}', '{{}}')"
            f" || COALESCE(EXCLUDED.spec #> '{{{key},{name}}}', '{{}}'))"
            f" - {param}::text[], '{{}}'::jsonb)"
        )
    return f"{part('vertex', vertex_param)}, {part('edge', edge_param)}"

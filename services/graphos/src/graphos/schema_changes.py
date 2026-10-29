"""Schema changes in the change feed: op ``schema``, kind ``tenant``.

A registration that changes the tenant's schema (labels, edge types,
declared pairs, label hierarchy, search indexes, resolve rules, property
indexes, generated columns, ``meta`` keys, display, property schema and deprecated
entries, ``strict_endpoints``, the property mode) is recorded with what it
added, removed, changed or switched; one that changes nothing (a consumer
re-registering at startup) records nothing. Removing a declared pair,
unregistering, restoring and deleting with the data are recorded too.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import asyncpg

from graphos import by_entry, changes, declared
from graphos.bootstrap import TenantSpec

# Spec keys kept per label and edge type (graphos.by_entry), recorded by entry.
BY_ENTRY = ("display", "property_schema", "deprecated")


def parts(spec: TenantSpec | None) -> dict[str, set[Any]]:
    """The items of each part of a spec, as the change records name them
    (by-entry parts as ``vertex:Person``); shared with ``graphos.schema_drift``."""
    return _sets(spec)


def shown(field: str, values: Iterable[Any]) -> list[Any]:
    """Items of one part as a record shows them (resolve rules by edge type)."""
    return _shown(field, values)


def _sets(spec: TenantSpec | None) -> dict[str, set[Any]]:
    if spec is None:
        return {}
    return {
        "vertex_labels": set(spec.vertex_labels),
        "edge_labels": set(spec.edge_labels),
        "edge_endpoints": {tuple(p) for p in spec.edge_endpoints},
        "label_hierarchy": {tuple(p) for p in spec.label_hierarchy},
        "search_indexes": set(spec.search_indexes),
        # A rule is named by its edge type; several may share one.
        "resolve_rules": {
            (name, json.dumps(rule, sort_keys=True, default=str))
            for name, rule in spec.resolve_rules
        },
        "property_indexes": {declared.describe_index(x) for x in spec.property_indexes},
        "generated_columns": {declared.describe_column(x) for x in spec.generated_columns},
        # By key: the notes themselves are free text, kept in the schema, not the feed.
        "meta": set(spec.meta),
        **{key: set(_entries(spec, key)) for key in BY_ENTRY},
    }


def _entries(spec: TenantSpec, key: str) -> dict[str, Any]:
    """``key``'s entries named ``vertex:Person`` / ``edge:WROTE``."""
    value = getattr(spec, key)
    return {
        f"{part}:{name}": entry
        for part in by_entry.PARTS
        for name, entry in (value.get(part) or {}).items()
    }


def _shown(field: str, values: Iterable[Any]) -> list[Any]:
    if field == "resolve_rules":
        return sorted({name for name, _ in values})          # the edge types
    return sorted(list(v) if isinstance(v, tuple) else v for v in values)


def diff(before: TenantSpec | None, after: TenantSpec) -> dict[str, Any] | None:
    """What a registration changed, or ``None`` when it changed nothing.

    ``added`` / ``removed`` per part (resolve rules by edge type),
    ``changed.meta`` for ``meta`` keys whose text changed, ``changed.display``
    and ``changed.property_schema`` for entries declared again differently,
    ``strict_endpoints`` and ``property_mode`` as ``[old, new]``, and
    ``registered: true`` for a new tenant.
    """
    old, new = _sets(before), _sets(after)
    detail: dict[str, Any] = {}
    for field, values in new.items():
        added = values - old.get(field, set())
        removed = old.get(field, set()) - values
        if added:
            detail.setdefault("added", {})[field] = _shown(field, added)
        if removed:
            detail.setdefault("removed", {})[field] = _shown(field, removed)
    if before is not None:
        rewritten = sorted(
            k for k, v in after.meta.items() if k in before.meta and before.meta[k] != v
        )
        if rewritten:
            detail["changed"] = {"meta": rewritten}
        # A search index declared again under its name replaces its definition.
        redefined = sorted(
            name for name, body in after.search_indexes.items()
            if name in before.search_indexes and before.search_indexes[name] != body
        )
        if redefined:
            detail.setdefault("changed", {})["search_indexes"] = redefined
        for key in BY_ENTRY:
            old_entries, new_entries = _entries(before, key), _entries(after, key)
            redeclared = sorted(
                k for k, v in new_entries.items() if k in old_entries and old_entries[k] != v
            )
            if redeclared:
                detail.setdefault("changed", {})[key] = redeclared
    strict_before = bool(before.strict_endpoints) if before else False
    if bool(after.strict_endpoints) != strict_before:
        detail["strict_endpoints"] = [strict_before, bool(after.strict_endpoints)]
    mode_before = before.property_schema.get("mode", "off") if before else "off"
    mode_after = after.property_schema.get("mode", "off")
    if mode_after != mode_before:
        detail["property_mode"] = [mode_before, mode_after]
    if before is None:
        detail["registered"] = True
    return detail or None


def size(detail: dict[str, Any]) -> int:
    """How many schema items a change touched (the record's ``count``), at least 1."""
    n = 0
    for part in ("added", "removed", "changed"):
        for values in detail.get(part, {}).values():
            n += len(values)
    return max(n + ("strict_endpoints" in detail) + ("property_mode" in detail), 1)


async def record(pool: asyncpg.Pool, tenant: str, detail: dict[str, Any]) -> None:
    """Record one schema change of ``tenant``."""
    async with pool.acquire() as conn:
        await changes.record(
            conn, tenant=tenant, op="schema", kind="tenant", count=size(detail), detail=detail,
        )

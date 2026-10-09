"""Schema changes in the change feed: op ``schema``, kind ``tenant``.

A registration that changes the tenant's schema (labels, edge types,
declared pairs, label hierarchy, search indexes, resolve rules, property
indexes, generated columns, ``strict_endpoints``) is recorded with what it
added, removed or switched; one that changes nothing (a consumer re-registering at startup) records
nothing.  Removing a declared pair and unregistering are recorded too.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import asyncpg

from graphos import changes, declared
from graphos.bootstrap import TenantSpec


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
    }


def _shown(field: str, values: Iterable[Any]) -> list[Any]:
    if field == "resolve_rules":
        return sorted({name for name, _ in values})          # the edge types
    return sorted(list(v) if isinstance(v, tuple) else v for v in values)


def diff(before: TenantSpec | None, after: TenantSpec) -> dict[str, Any] | None:
    """What a registration changed, or ``None`` when it changed nothing.

    ``added`` / ``removed`` per part (resolve rules by edge type),
    ``strict_endpoints`` as ``[old, new]``, and
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
    strict_before = bool(before.strict_endpoints) if before else False
    if bool(after.strict_endpoints) != strict_before:
        detail["strict_endpoints"] = [strict_before, bool(after.strict_endpoints)]
    if before is None:
        detail["registered"] = True
    return detail or None


def size(detail: dict[str, Any]) -> int:
    """How many schema items a change touched (the record's ``count``), at least 1."""
    n = 0
    for part in ("added", "removed"):
        for values in detail.get(part, {}).values():
            n += len(values)
    return max(n + ("strict_endpoints" in detail), 1)


async def record(pool: asyncpg.Pool, tenant: str, detail: dict[str, Any]) -> None:
    """Record one schema change of ``tenant``."""
    async with pool.acquire() as conn:
        await changes.record(
            conn, tenant=tenant, op="schema", kind="tenant", count=size(detail), detail=detail,
        )

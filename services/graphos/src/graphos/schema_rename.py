"""Renaming a vertex label or an edge type in place (ADR-0009 §2).

AGE has no function for it.  A label is a row of ``ag_catalog.ag_label``
(its ``name`` and ``seq_name``), a table, a sequence and an ``id`` column
whose default names the label; renaming those four in one transaction keeps
the table's OID, so its rows, their graph ids, the edges pointing at them,
inheritance (the label hierarchy), triggers and constraints all stay as they
are, and every connection sees the new name at once (tested on AGE 1.8).

Around it, in the same transaction and under the lock tenant bootstrap
takes: the indexes graphos named after the label, everything in
``graphos_meta`` that names it (the spec, declared pairs, the property
catalogue, the community map), the ``renames`` history, one ``schema``
change record, and ``NOTIFY`` so every replica reloads.  The indexes whose
name could not follow (hashed long names) are built again afterwards under
the new name by the usual idempotent ensures, and the leftovers dropped.

Saved queries are reported, never rewritten: Cypher that names an unknown
label returns nothing instead of failing.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import asyncpg

from graphos import changes
from graphos.bootstrap import (
    TENANTS_CHANNEL,
    TenantSpec,
    _spec_to_jsonable,
    bootstrap_tenant,
    ensure_search_indexes,
    load_spec,
    remember,
    spec_from_json,
)
from graphos.lifecycle import DROP_LOCK_TIMEOUT, TenantBusyError
from graphos.resolver.indexer import ensure_resolve_rule_indexes

log = logging.getLogger(__name__)

_NAME = re.compile(r"^[A-Z][A-Za-z0-9_]{0,62}$")


class RenameError(ValueError):
    """A rename that cannot be done as asked (answered 400)."""


@dataclass
class RenamePlan:
    """What renaming would change."""

    kind: str
    old: str
    new: str
    spec_parts: list[str] = field(default_factory=list)
    indexes: list[tuple[str, str]] = field(default_factory=list)
    endpoints: int = 0
    rows: int | None = None
    saved_queries: list[str] = field(default_factory=list)


# ── The spec, renamed ──────────────────────────────────────────────────────


def _swap(values: Iterable[str], old: str, new: str) -> tuple[str, ...]:
    return tuple(new if v == old else v for v in values)


def _rekey(entries: Mapping[str, Any], old: str, new: str) -> dict[str, Any]:
    return {(new if k == old else k): v for k, v in entries.items()}


def _part_rekeyed(value: Mapping[str, Any], part: str, old: str, new: str) -> dict[str, Any]:
    """A by-entry spec key (``display``, ``property_schema``, ``deprecated``) with
    ``part``'s entry ``old`` renamed."""
    out = dict(value)
    if part in out:
        out[part] = _rekey(out[part], old, new)
    return out


def _replacements_renamed(deprecated: Mapping[str, Any], old: str, new: str) -> dict[str, Any]:
    """Deprecations whose ``replaced_by`` named ``old`` now name ``new``."""
    out: dict[str, Any] = {}
    for part, entries in deprecated.items():
        out[part] = {
            name: {**entry, "replaced_by": new} if entry.get("replaced_by") == old else entry
            for name, entry in entries.items()
        }
    return out


def _strs(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def renamed_spec(
    spec: TenantSpec, kind: str, old: str, new: str, entry: dict[str, Any],
) -> TenantSpec:
    """``spec`` with the vertex label (``kind="label"``) or edge type ``old`` named
    ``new`` everywhere, and ``entry`` added to its renames."""
    part = "vertex" if kind == "label" else "edge"
    spec = replace(
        spec,
        display=_part_rekeyed(spec.display, part, old, new),
        property_schema=_part_rekeyed(spec.property_schema, part, old, new),
        deprecated=_replacements_renamed(
            _part_rekeyed(spec.deprecated, part, old, new), old, new,
        ),
        renames=(*spec.renames, entry),
    )
    if kind != "label":
        return replace(
            spec,
            edge_labels=_swap(spec.edge_labels, old, new),
            resolve_rules=tuple(
                (new if t == old else t, dict(rule)) for t, rule in spec.resolve_rules
            ),
            edge_endpoints=tuple(
                (new if t == old else t, s, d) for t, s, d in spec.edge_endpoints
            ),
        )
    rules: list[tuple[str, dict[str, object]]] = []
    for edge_type, rule in spec.resolve_rules:
        allowed = rule.get("require_dst_label")
        rules.append((
            edge_type,
            {**rule, "require_dst_label": list(_swap(_strs(allowed), old, new))}
            if isinstance(allowed, list) else dict(rule),
        ))
    return replace(
        spec,
        vertex_labels=_swap(spec.vertex_labels, old, new),
        label_hierarchy=tuple(
            (new if c == old else c, new if p == old else p) for c, p in spec.label_hierarchy
        ),
        property_indexes=tuple(
            {**x, "label": new} if x.get("label") == old else dict(x)
            for x in spec.property_indexes
        ),
        generated_columns=tuple(
            {**x, "label": new} if x.get("label") == old else dict(x)
            for x in spec.generated_columns
        ),
        search_indexes={
            name: {**body, "labels": list(_swap(_strs(body.get("labels")), old, new))}
            if isinstance(body.get("labels"), list) else dict(body)
            for name, body in spec.search_indexes.items()
        },
        resolve_rules=tuple(rules),
        edge_endpoints=tuple(
            (t, new if s == old else s, new if d == old else d) for t, s, d in spec.edge_endpoints
        ),
    )


def spec_parts_named(spec: TenantSpec, kind: str, old: str) -> list[str]:
    """The spec's parts that name ``old`` (what a dry run reports)."""
    part = "vertex" if kind == "label" else "edge"
    found: list[str] = []
    if kind == "label":
        if any(old in pair for pair in spec.label_hierarchy):
            found.append("label_hierarchy")
        if any(x.get("label") == old for x in spec.property_indexes):
            found.append("property_indexes")
        if any(x.get("label") == old for x in spec.generated_columns):
            found.append("generated_columns")
        if any(old in _strs(b.get("labels")) for b in spec.search_indexes.values()):
            found.append("search_indexes")
        if any(old in _strs(r.get("require_dst_label")) for _, r in spec.resolve_rules):
            found.append("resolve_rules")
    elif any(t == old for t, _ in spec.resolve_rules):
        found.append("resolve_rules")
    for key in ("display", "property_schema", "deprecated"):
        if old in (getattr(spec, key).get(part) or {}):
            found.append(key)
    return found


# ── Saved queries that name it ─────────────────────────────────────────────


def mentions(query: str, name: str) -> bool:
    """Whether Cypher text names the label or type ``name`` (``:Name``, ``:`Name```)."""
    pattern = rf":\s*`?{re.escape(name)}`?(?![A-Za-z0-9_])"
    return re.search(pattern, query) is not None


async def saved_queries_naming(conn: asyncpg.Connection, tenant: str, name: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT name, definition->>'query' AS query FROM graphos_meta.saved_queries "
        "WHERE tenant = $1 ORDER BY name;",
        tenant,
    )
    return [r["name"] for r in rows if r["query"] and mentions(r["query"], name)]


# ── Checks ─────────────────────────────────────────────────────────────────


def check(spec: TenantSpec, kind: str, old: str, new: str) -> None:
    """Refuse a rename that cannot be done (:class:`RenameError`)."""
    names = spec.vertex_labels if kind == "label" else spec.edge_labels
    what = "vertex label" if kind == "label" else "edge type"
    if old not in names:
        raise RenameError(f"{old!r} is not a {what} of tenant '{spec.name}'")
    if not _NAME.fullmatch(new):
        raise RenameError(f"{new!r} must match {_NAME.pattern}")
    if new == old:
        raise RenameError("the new name is the old one")
    if new in spec.vertex_labels or new in spec.edge_labels:
        raise RenameError(f"{new!r} is already a label or edge type of the tenant")


# ── Indexes ────────────────────────────────────────────────────────────────


async def _indexes_on(conn: asyncpg.Connection, tenant: str, table: str) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT i.relname AS name
        FROM pg_index x
        JOIN pg_class i ON i.oid = x.indexrelid
        JOIN pg_class t ON t.oid = x.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = $1 AND t.relname = $2
        ORDER BY 1;
        """,
        tenant, table,
    )
    return [r["name"] for r in rows]


def _index_renames(names: Iterable[str], old: str, new: str) -> list[tuple[str, str]]:
    """The indexes graphos named after the label (``<Label>_…``, the resolver's
    ``idx_<Label>_…``), each with its name under the new label."""
    out: list[tuple[str, str]] = []
    for name in names:
        for prefix in (f"{old}_", f"idx_{old}_"):
            if name.startswith(prefix):
                renamed = prefix.replace(old, new, 1) + name[len(prefix):]
                if len(renamed) <= 63:  # noqa: PLR2004 -- Postgres identifier limit
                    out.append((name, renamed))
                break
    return out


async def _drop_duplicate_indexes(conn: asyncpg.Connection, tenant: str, table: str) -> int:
    """Drop the indexes another index on the table already is, name aside (what
    rebuilding a hashed name under the new label leaves behind)."""
    rows = await conn.fetch(
        """
        SELECT i.relname AS name,
               regexp_replace(
                   pg_get_indexdef(x.indexrelid), '^CREATE (?:UNIQUE )?INDEX \\S+ ', ''
               ) AS body,
               x.indisunique AS is_unique
        FROM pg_index x
        JOIN pg_class i ON i.oid = x.indexrelid
        JOIN pg_class t ON t.oid = x.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = $1 AND t.relname = $2 AND NOT x.indisprimary
        ORDER BY i.relname;
        """,
        tenant, table,
    )
    seen: dict[tuple[str, bool], str] = {}
    dropped = 0
    for r in rows:
        key = (r["body"], r["is_unique"])
        if key in seen:
            await conn.execute(f'DROP INDEX IF EXISTS "{tenant}"."{r["name"]}";')
            dropped += 1
        else:
            seen[key] = r["name"]
    return dropped


# ── The rename ─────────────────────────────────────────────────────────────


async def plan(pool: asyncpg.Pool, spec: TenantSpec, kind: str, old: str, new: str) -> RenamePlan:
    """What renaming would change, writing nothing (a dry run)."""
    check(spec, kind, old, new)
    async with pool.acquire() as conn:
        indexes = _index_renames(await _indexes_on(conn, spec.name, old), old, new)
        endpoints = await conn.fetchval(
            "SELECT count(*) FROM graphos_meta.edge_endpoints WHERE tenant = $1 AND "
            + ("(src_label = $2 OR dst_label = $2)" if kind == "label" else "edge_type = $2")
            + ";",
            spec.name, old,
        )
        rows = await conn.fetchval(f'SELECT count(*) FROM ONLY "{spec.name}"."{old}";')
        saved = await saved_queries_naming(conn, spec.name, old)
    return RenamePlan(
        kind=kind, old=old, new=new, spec_parts=spec_parts_named(spec, kind, old),
        indexes=indexes, endpoints=int(endpoints), rows=int(rows), saved_queries=saved,
    )


async def rename(  # noqa: PLR0913 -- one rename's inputs
    pool: asyncpg.Pool, spec: TenantSpec, kind: str, old: str, new: str,
    *, by: str | None, vertex_graphid_indexes: bool = False,
) -> RenamePlan:
    """Rename the vertex label or edge type ``old`` to ``new`` in place, in one
    transaction (ADR-0009 §2).

    Raises:
        RenameError: the rename cannot be done as asked.
        TenantBusyError: a job is queued or running on the tenant, a reconcile
            holds the edge type, or the tables stay in use for
            :data:`graphos.lifecycle.DROP_LOCK_TIMEOUT`.
    """
    check(spec, kind, old, new)
    tenant = spec.name
    entry = {"kind": kind, "from": old, "to": new, "at": datetime.now(UTC).isoformat(), "by": by}
    found = RenamePlan(kind=kind, old=old, new=new)
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await conn.execute(f"SET LOCAL lock_timeout = '{DROP_LOCK_TIMEOUT}';")
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0));", tenant,
                )
                # The stored spec, as it is now (another replica may have changed it).
                stored = spec_from_json(await conn.fetchval(
                    "SELECT spec::text FROM graphos_meta.tenants WHERE name = $1 FOR UPDATE;",
                    tenant,
                ))
                check(stored, kind, old, new)
                after = renamed_spec(stored, kind, old, new, entry)
                found.spec_parts = spec_parts_named(stored, kind, old)
                await _refuse_busy(conn, tenant, kind, old)
                found.indexes = await _rename_in_age(conn, tenant, old, new)
                found.endpoints = await _rename_in_meta(conn, after, kind, old, new)
                found.saved_queries = await saved_queries_naming(conn, tenant, old)
                await changes.record(
                    conn, tenant=tenant, op="schema", kind="tenant", count=1,
                    detail={"renamed": {"kind": kind, "from": old, "to": new}},
                )
                await conn.execute("SELECT pg_notify($1, $2);", TENANTS_CHANNEL, tenant)
        except asyncpg.LockNotAvailableError as exc:
            raise TenantBusyError(
                f"tenant '{tenant}' is in use (an export, an import or a long query holds its "
                "tables); try again when it ends"
            ) from exc
    loaded = await load_spec(pool, tenant) or after
    remember(loaded)
    await _ensure_after(pool, loaded, new, vertex_graphid_indexes=vertex_graphid_indexes)
    log.info("schema rename", extra={"tenant": tenant, "kind": kind, "from": old, "to": new})
    return found


async def _refuse_busy(conn: asyncpg.Connection, tenant: str, kind: str, old: str) -> None:
    active = await conn.fetchval(
        "SELECT count(*) FROM graphos_meta.jobs "
        "WHERE tenant = $1 AND status IN ('queued', 'running');",
        tenant,
    )
    if active:
        raise TenantBusyError(
            f"{active} job(s) queued or running on tenant '{tenant}': "
            "cancel them or let them finish first"
        )
    if kind == "edge_type" and not await conn.fetchval(
        "SELECT pg_try_advisory_xact_lock(hashtextextended($1, 0));",
        f"graphos.reconcile:{tenant}:{old}",
    ):
        raise TenantBusyError(f"a reconcile of {old!r} is running; try again when it ends")


async def _rename_in_age(
    conn: asyncpg.Connection, tenant: str, old: str, new: str,
) -> list[tuple[str, str]]:
    """AGE's catalogue, the table, its sequence, the ``id`` default and graphos's indexes."""
    seq_old, seq_new = f"{old}_id_seq", f"{new}_id_seq"
    updated = await conn.execute(
        """
        UPDATE ag_catalog.ag_label SET name = $3, seq_name = $4
        WHERE graph = (SELECT graphid FROM ag_catalog.ag_graph WHERE name = $1) AND name = $2;
        """,
        tenant, old, new, seq_new,
    )
    if updated != "UPDATE 1":
        raise RenameError(f"{old!r} is not a label of the graph '{tenant}'")
    await conn.execute(f'ALTER TABLE "{tenant}"."{old}" RENAME TO "{new}";')
    await conn.execute(f'ALTER SEQUENCE "{tenant}"."{seq_old}" RENAME TO "{seq_new}";')
    await conn.execute(
        f'ALTER TABLE "{tenant}"."{new}" ALTER COLUMN id SET DEFAULT '
        f"ag_catalog._graphid(ag_catalog._label_id('{tenant}'::name, '{new}'::name)::integer, "
        f"nextval('\"{tenant}\".\"{seq_new}\"'::regclass));"
    )
    renamed = _index_renames(await _indexes_on(conn, tenant, new), old, new)
    for before, after in renamed:
        await conn.execute(f'ALTER INDEX "{tenant}"."{before}" RENAME TO "{after}";')
    return renamed


async def _rename_in_meta(
    conn: asyncpg.Connection, after: TenantSpec, kind: str, old: str, new: str,
) -> int:
    """The spec and every ``graphos_meta`` row naming the label or type; the
    declared pairs renamed."""
    tenant = after.name
    await conn.execute(
        "UPDATE graphos_meta.tenants SET spec = $2::jsonb, updated_at = now() WHERE name = $1;",
        tenant, json.dumps(_spec_to_jsonable(after)),
    )
    if kind == "label":
        moved = await conn.execute(
            """
            UPDATE graphos_meta.edge_endpoints
            SET src_label = CASE WHEN src_label = $2 THEN $3 ELSE src_label END,
                dst_label = CASE WHEN dst_label = $2 THEN $3 ELSE dst_label END
            WHERE tenant = $1 AND (src_label = $2 OR dst_label = $2);
            """,
            tenant, old, new,
        )
        await conn.execute(
            "UPDATE graphos_meta.node_properties SET label = $3 WHERE tenant = $1 AND label = $2;",
            tenant, old, new,
        )
        await conn.execute(
            """
            UPDATE graphos_meta.community_map
            SET labels = (labels - $2::text) || jsonb_build_object($3::text, labels -> $2::text)
            WHERE tenant = $1 AND labels ? $2::text;
            """,
            tenant, old, new,
        )
    else:
        moved = await conn.execute(
            "UPDATE graphos_meta.edge_endpoints SET edge_type = $3 "
            "WHERE tenant = $1 AND edge_type = $2;",
            tenant, old, new,
        )
        await conn.execute(
            "UPDATE graphos_meta.community_links SET type = $3 WHERE tenant = $1 AND type = $2;",
            tenant, old, new,
        )
    return int(moved.split()[-1])


async def _ensure_after(
    pool: asyncpg.Pool, spec: TenantSpec, new: str, *, vertex_graphid_indexes: bool,
) -> None:
    """Indexes under the new name that a rename could not carry over (hashed long
    names) are built again, and the copies they replace dropped.  Best effort:
    the rename is done; a failure here leaves an index to build at the next
    registration."""
    try:
        await bootstrap_tenant(pool, spec, vertex_graphid_indexes=vertex_graphid_indexes)
        if spec.search_indexes:
            await ensure_search_indexes(pool, spec)
        await ensure_resolve_rule_indexes(pool, tenant=spec.name, spec=spec)
        async with pool.acquire() as conn:
            dropped = await _drop_duplicate_indexes(conn, spec.name, new)
        if dropped:
            log.info("schema rename: duplicate indexes dropped",
                     extra={"tenant": spec.name, "label": new, "dropped": dropped})
    except Exception:  # noqa: BLE001 -- never fail a done rename over an index
        log.exception("schema rename: rebuilding indexes failed", extra={"tenant": spec.name})

"""Which property names each vertex label's nodes carry, kept as nodes are written.

``graphos_meta.node_properties`` holds one row per (tenant, label, property)
that some node of the label has carried.  The write path adds the names it
writes in the same transaction as the nodes (:func:`note`, or
:data:`NOTE_CTE` inside the upsert statement), so the table is current the
moment the nodes commit.  The resolver's catalogue reads it instead of
listing the keys of every row.

The table only grows: a name stays after its last carrier is patched or
deleted.  That over-approximates, which the resolver tolerates (it then
finds no matching rows); :func:`backfill` with ``rebuild=True`` trims it.

A tenant is *complete* (``graphos_meta.node_properties_complete``) once
its rows cover every node: at once for a graph created after this table
existed (:func:`mark_new`), and after one :func:`backfill` for an older
one, which reads every node once.  :func:`ensure_complete` runs that
backfill when a lookup needs it, and the API starts it in the background
for every incomplete tenant at startup.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import asyncpg

from graphos.db import DDL_TIMEOUT_S, allow_long_statements

log = logging.getLogger(__name__)

_INSERT = (
    "INSERT INTO graphos_meta.node_properties (tenant, label, property) "
    "SELECT $1, $2, p FROM unnest($3::text[]) AS p ON CONFLICT DO NOTHING"
)

# For a statement that already writes the nodes: ``WITH`` this, then the
# write, with the tenant, the label and the names as ``$1``-``$3``.  A name
# already recorded costs one unique-index lookup and writes nothing.
NOTE_CTE = f"WITH noted_properties AS ({_INSERT})\n"


def _lock_key(tenant: str) -> str:
    return f"graphos-node-properties:{tenant}"


async def note(conn: asyncpg.Connection, tenant: str, label: str, names: Iterable[str]) -> None:
    """Record that nodes of ``label`` now carry ``names``; call inside the write's transaction."""
    wanted = sorted(set(names))
    if wanted:
        await conn.execute(f"{_INSERT};", tenant, label, wanted)


async def mark_new(conn: asyncpg.Connection, tenant: str) -> None:
    """A graph just created holds no nodes: its (empty) record is complete.

    Rows left by an earlier graph of the same name are removed.
    """
    await conn.execute("DELETE FROM graphos_meta.node_properties WHERE tenant = $1;", tenant)
    await conn.execute(
        "INSERT INTO graphos_meta.node_properties_complete (tenant) VALUES ($1) "
        "ON CONFLICT (tenant) DO UPDATE SET since = now();",
        tenant,
    )


async def is_complete(conn: asyncpg.Connection, tenant: str) -> bool:
    return bool(await conn.fetchval(
        "SELECT 1 FROM graphos_meta.node_properties_complete WHERE tenant = $1;", tenant,
    ))


async def labels_by_property(
    conn: asyncpg.Connection, tenant: str, names: Iterable[str] | None = None,
) -> dict[str, set[str]]:
    """``{property: labels}`` for ``names``, or for every recorded name."""
    if names is None:
        rows = await conn.fetch(
            "SELECT property, label FROM graphos_meta.node_properties WHERE tenant = $1;",
            tenant,
        )
    else:
        rows = await conn.fetch(
            "SELECT property, label FROM graphos_meta.node_properties "
            "WHERE tenant = $1 AND property = ANY($2::text[]);",
            tenant, sorted(set(names)),
        )
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(row["property"], set()).add(row["label"])
    return out


async def ensure_complete(pool: asyncpg.Pool, tenant: str) -> None:
    """Backfill the tenant first if its record does not cover every node yet."""
    async with pool.acquire() as conn:
        if await is_complete(conn, tenant):
            return
    await backfill(pool, tenant)


async def backfill(pool: asyncpg.Pool, tenant: str, *, rebuild: bool = False) -> None:
    """Record the property names of every node of the tenant, then mark it complete.

    Reads every node once.  Each label is read in its own read-only
    statement, which holds back no one's writes (nor the change feed), and
    its names are added in a short write; writes meanwhile record their own
    names.  ``rebuild`` first drops the tenant's rows, so names no node
    carries any more disappear; lookups wait for the rebuild to finish.
    Concurrent callers (other requests, other replicas) wait for one run.
    """
    async with pool.acquire() as conn:
        await allow_long_statements(conn, DDL_TIMEOUT_S)
        await conn.execute("SELECT pg_advisory_lock(hashtextextended($1, 0));", _lock_key(tenant))
        try:
            if not rebuild and await is_complete(conn, tenant):
                return
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM graphos_meta.node_properties_complete WHERE tenant = $1;", tenant,
                )
                if rebuild:
                    await conn.execute(
                        "DELETE FROM graphos_meta.node_properties WHERE tenant = $1;", tenant,
                    )
            labels = [r["relname"] for r in await conn.fetch(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_inherits i ON i.inhrelid = c.oid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE n.nspname = $1 AND c.relkind = 'r' AND p.relname = '_ag_label_vertex';",
                tenant,
            )]
            for label in labels:
                keys = await conn.fetchval(
                    f'SELECT array_agg(DISTINCT k) FROM ONLY "{tenant}"."{label}", '
                    "LATERAL jsonb_object_keys((properties::text)::jsonb) AS k;",
                )
                await note(conn, tenant, label, keys or [])
            await conn.execute(
                "INSERT INTO graphos_meta.node_properties_complete (tenant) VALUES ($1) "
                "ON CONFLICT (tenant) DO UPDATE SET since = now();",
                tenant,
            )
            log.info("node properties recorded", extra={"tenant": tenant, "labels": len(labels)})
        finally:
            await conn.execute(
                "SELECT pg_advisory_unlock(hashtextextended($1, 0));", _lock_key(tenant),
            )


async def backfill_incomplete(pool: asyncpg.Pool, tenants: Iterable[str]) -> None:
    """Backfill each of ``tenants`` that is not complete yet, one at a time.

    Run in the background at startup; a failure is logged and the tenant is
    backfilled when a lookup next needs it.
    """
    for tenant in tenants:
        try:
            await ensure_complete(pool, tenant)
        except (asyncpg.PostgresError, OSError) as exc:
            log.warning(
                "node properties backfill failed", extra={"tenant": tenant, "error": str(exc)},
            )

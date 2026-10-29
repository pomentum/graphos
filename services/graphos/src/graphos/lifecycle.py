"""A tenant's life after registration: unregistered, restored, deleted.

Unregistering (:func:`graphos.bootstrap.unregister_tenant`) keeps the data
and sets the registration aside in ``graphos_meta.unregistered_tenants``;
:func:`restore_tenant` brings it back as it was.  :func:`drop_tenant_data`
deletes a tenant for good: its AGE graph and every row graphos keeps about
it.  Grants on the tenant's name stay while it is unregistered, so its owner
can restore it; they go when its data does.

A graph with no registration at all (unregistered before graphos kept the
registration aside) is an *orphan*: only an admin may take it over or delete
it, so a name someone else may create can never hand them old data.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import asyncpg

from graphos.bootstrap import (
    TENANTS_CHANNEL,
    TenantSpec,
    forget,
    load_spec,
    remember,
    spec_from_row,
)

log = logging.getLogger(__name__)

# How long deleting a tenant's data waits for the tables in use (an export,
# an import, a long query) before it gives up and says so.
DROP_LOCK_TIMEOUT = "5s"

State = Literal["registered", "unregistered", "orphan"]


class TenantBusyError(Exception):
    """The tenant is in use (a job, or a statement holding its tables)."""


@dataclass(frozen=True, slots=True)
class Unregistered:
    """A tenant whose data is kept without a registration."""

    name: str
    kind: Literal["unregistered", "orphan"]
    vertex_labels: int
    edge_labels: int
    created_at: datetime | None
    unregistered_at: datetime | None
    unregistered_by: str | None


async def tenant_state(conn: asyncpg.Connection, name: str) -> State | None:
    """Where ``name`` stands: registered, unregistered (data and registration
    kept), an orphan graph, or None (nothing by that name)."""
    if await conn.fetchval("SELECT 1 FROM graphos_meta.tenants WHERE name = $1;", name):
        return "registered"
    if await conn.fetchval(
        "SELECT 1 FROM graphos_meta.unregistered_tenants WHERE name = $1;", name,
    ):
        return "unregistered"
    if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", name):
        return "orphan"
    return None


async def list_unregistered(pool: asyncpg.Pool) -> list[Unregistered]:
    """Unregistered tenants and orphan graphs, by name."""
    async with pool.acquire() as conn:
        kept = await conn.fetch(
            """
            SELECT name, created_at, unregistered_at, unregistered_by,
                   jsonb_array_length(COALESCE(spec->'vertex_labels', '[]')) AS n_vertex,
                   jsonb_array_length(COALESCE(spec->'edge_labels', '[]')) AS n_edge
            FROM graphos_meta.unregistered_tenants ORDER BY name;
            """,
        )
        orphans = await conn.fetch(
            """
            SELECT g.name::text AS name
            FROM ag_catalog.ag_graph g
            WHERE g.name::text NOT IN (SELECT name FROM graphos_meta.tenants)
              AND g.name::text NOT IN (SELECT name FROM graphos_meta.unregistered_tenants)
            ORDER BY 1;
            """,
        )
    out = [
        Unregistered(
            name=r["name"], kind="unregistered", vertex_labels=r["n_vertex"],
            edge_labels=r["n_edge"], created_at=r["created_at"],
            unregistered_at=r["unregistered_at"], unregistered_by=r["unregistered_by"],
        )
        for r in kept
    ]
    out += [
        Unregistered(
            name=r["name"], kind="orphan", vertex_labels=0, edge_labels=0,
            created_at=None, unregistered_at=None, unregistered_by=None,
        )
        for r in orphans
    ]
    return sorted(out, key=lambda u: u.name)


async def restore_tenant(pool: asyncpg.Pool, name: str) -> bool:
    """Register an unregistered tenant again, as it was.  False if it is not one."""
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            DELETE FROM graphos_meta.unregistered_tenants WHERE name = $1
            RETURNING spec::text AS spec, endpoints::text AS endpoints,
                      strict_endpoints, schema_manager, schema_manager_since, created_at;
            """,
            name,
        )
        if row is None:
            return False
        await conn.execute(
            "INSERT INTO graphos_meta.tenants (name, spec, created_at) VALUES ($1, $2::jsonb, $3);",
            name, row["spec"], row["created_at"],
        )
        pairs = [(name, t, s, d) for t, s, d in json.loads(row["endpoints"] or "[]")]
        if pairs:
            await conn.executemany(
                """
                INSERT INTO graphos_meta.edge_endpoints (tenant, edge_type, src_label, dst_label)
                VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING;
                """,
                pairs,
            )
        if row["strict_endpoints"] or row["schema_manager"]:
            await conn.execute(
                """
                INSERT INTO graphos_meta.tenant_settings
                    (tenant, strict_endpoints, schema_manager, schema_manager_since)
                VALUES ($1, $2, $3, CASE WHEN $3::text IS NULL THEN NULL
                                         ELSE COALESCE($4, now()) END)
                ON CONFLICT (tenant) DO UPDATE
                SET strict_endpoints = EXCLUDED.strict_endpoints,
                    schema_manager = EXCLUDED.schema_manager,
                    schema_manager_since = EXCLUDED.schema_manager_since, updated_at = now();
                """,
                name, bool(row["strict_endpoints"]), row["schema_manager"],
                row["schema_manager_since"],
            )
        await conn.execute("SELECT pg_notify($1, $2);", TENANTS_CHANNEL, name)
    spec = await load_spec(pool, name)
    if spec is not None:
        remember(spec)
    return True


async def kept_registration(pool: asyncpg.Pool, name: str) -> dict[str, object] | None:
    """An unregistered tenant's kept row as it is, for :func:`put_back`."""
    row = await pool.fetchrow(
        "SELECT * FROM graphos_meta.unregistered_tenants WHERE name = $1;", name,
    )
    return None if row is None else dict(row)


async def put_back(pool: asyncpg.Pool, kept: dict[str, object]) -> None:
    """Undo a restore that the registration restoring it could not finish:
    the tenant is unregistered again with the very row it was restored from
    (its schema, pairs, settings, manager, and when and by whom it was
    unregistered), not with what the registration wrote meanwhile."""
    name = str(kept["name"])
    columns = list(kept)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", name)
        await conn.execute("DELETE FROM graphos_meta.edge_endpoints WHERE tenant = $1;", name)
        await conn.execute("DELETE FROM graphos_meta.tenant_settings WHERE tenant = $1;", name)
        await conn.execute(
            f"INSERT INTO graphos_meta.unregistered_tenants ({', '.join(columns)}) "
            f"VALUES ({', '.join(f'${i}' for i in range(1, len(columns) + 1))}) "
            "ON CONFLICT (name) DO NOTHING;",
            *kept.values(),
        )
        await conn.execute("SELECT pg_notify($1, $2);", TENANTS_CHANNEL, name)
    forget(name)
    log.warning("restore undone: the registration restoring it failed", extra={"tenant": name})


# Every row graphos keeps about a tenant besides its graph.  Jobs still queued
# or running refuse the deletion before any of this runs.
_TENANT_ROWS = (
    "DELETE FROM graphos_meta.tenants WHERE name = $1;",
    "DELETE FROM graphos_meta.unregistered_tenants WHERE name = $1;",
    "DELETE FROM graphos_meta.edge_endpoints WHERE tenant = $1;",
    "DELETE FROM graphos_meta.tenant_settings WHERE tenant = $1;",
    "DELETE FROM graphos_meta.schema_removals WHERE tenant = $1;",
    "DELETE FROM graphos_meta.cache WHERE tenant = $1;",
    "DELETE FROM graphos_meta.node_properties WHERE tenant = $1;",
    "DELETE FROM graphos_meta.node_properties_complete WHERE tenant = $1;",
    "DELETE FROM graphos_meta.community_map WHERE tenant = $1;",
    "DELETE FROM graphos_meta.community_links WHERE tenant = $1;",
    "DELETE FROM graphos_meta.community_map_runs WHERE tenant = $1;",
    "DELETE FROM graphos_meta.analysis_runs WHERE tenant = $1;",
    "DELETE FROM graphos_meta.jobs WHERE tenant = $1;",
    "DELETE FROM graphos_meta.saved_queries WHERE tenant = $1;",
)

# The tenant's change records go; its horizon moves past them, so a reader
# still holding a cursor into them is told to start again (410).
_DROP_CHANGES_SQL = """
WITH gone AS (
    DELETE FROM graphos_meta.changes WHERE tenant = $1 RETURNING txid, seq
), last AS (
    SELECT txid, seq FROM gone ORDER BY txid DESC, seq DESC LIMIT 1
)
INSERT INTO graphos_meta.change_horizon AS h (tenant, txid, seq)
SELECT $1, txid, seq FROM last
ON CONFLICT (tenant) DO UPDATE SET txid = EXCLUDED.txid, seq = EXCLUDED.seq
WHERE (h.txid, h.seq) < (EXCLUDED.txid, EXCLUDED.seq);
"""


async def drop_tenant_data(pool: asyncpg.Pool, name: str) -> bool:
    """Delete a tenant for good: its graph, its data and graphos's rows about
    it, in one transaction.  False if there is nothing by that name.

    Raises:
        TenantBusyError: a job is queued or running on it, or its tables stay
            in use (an export, an import, a long query) for
            :data:`DROP_LOCK_TIMEOUT`.
    """
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await conn.execute(f"SET LOCAL lock_timeout = '{DROP_LOCK_TIMEOUT}';")
                # The lock tenant bootstrap holds: no registration runs meanwhile.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0));", name)
                state = await tenant_state(conn, name)
                if state is None:
                    return False
                active = await conn.fetchval(
                    "SELECT count(*) FROM graphos_meta.jobs "
                    "WHERE tenant = $1 AND status IN ('queued', 'running');",
                    name,
                )
                if active:
                    raise TenantBusyError(
                        f"{active} job(s) queued or running on tenant '{name}': "
                        "cancel them or let them finish first"
                    )
                for sql in _TENANT_ROWS:
                    await conn.execute(sql, name)
                await conn.execute(_DROP_CHANGES_SQL, name)
                if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", name):
                    await conn.execute("SELECT ag_catalog.drop_graph($1::name, true);", name)
                await conn.execute("SELECT pg_notify($1, $2);", TENANTS_CHANNEL, name)
        except asyncpg.LockNotAvailableError as exc:
            raise TenantBusyError(
                f"tenant '{name}' is in use (an export, an import or a long query holds its "
                "tables); try again when it ends"
            ) from exc
    forget(name)
    log.warning("tenant deleted with its data", extra={"tenant": name, "was": state})
    return True


async def unregistered_spec(pool: asyncpg.Pool, name: str) -> TenantSpec | None:
    """The registration an unregistered tenant would be restored with."""
    row = await pool.fetchrow(
        """
        SELECT name, spec::text AS spec, endpoints::text AS endpoints, strict_endpoints,
               schema_manager, schema_manager_since
        FROM graphos_meta.unregistered_tenants WHERE name = $1;
        """,
        name,
    )
    return None if row is None else spec_from_row(row)

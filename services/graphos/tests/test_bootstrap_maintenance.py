"""Tenant setup: graphid indexes and statistics for never-analyzed tables.

DB-gated.
"""

from __future__ import annotations

import asyncpg
import pytest

from graphos.bootstrap import TenantSpec, bootstrap_tenant, register_tenant

from .conftest import requires_db

T = "graphos_test_bootstrap_maint"
SPEC = TenantSpec(name=T, vertex_labels=("Doc", "Tag"), edge_labels=("CITES", "TAGGED"))


async def _graphid_indexes(pool: asyncpg.Pool) -> dict[str, bool]:
    """table -> is its ``<table>_graphid`` index valid."""
    rows = await pool.fetch(
        "SELECT t.relname AS tbl, i.indisvalid AS ok FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_class t ON t.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relname = t.relname || '_graphid';",
        T,
    )
    return {r["tbl"]: r["ok"] for r in rows}


@requires_db
@pytest.mark.asyncio
async def test_edge_tables_get_a_graphid_index_vertex_tables_on_request(
    db_pool: asyncpg.Pool,
) -> None:
    await register_tenant(db_pool, SPEC)
    for label in ("Doc", "Tag"):                       # from an earlier run
        await db_pool.execute(f'DROP INDEX IF EXISTS "{T}"."{label}_graphid";')
    assert await _graphid_indexes(db_pool) == {"CITES": True, "TAGGED": True}
    await bootstrap_tenant(db_pool, SPEC, vertex_graphid_indexes=True)
    assert await _graphid_indexes(db_pool) == {
        "CITES": True, "TAGGED": True, "Doc": True, "Tag": True,
    }


@requires_db
@pytest.mark.asyncio
async def test_an_interrupted_index_build_is_redone(db_pool: asyncpg.Pool) -> None:
    await register_tenant(db_pool, SPEC)
    # What an interrupted CREATE INDEX CONCURRENTLY leaves behind.
    await db_pool.execute(
        "UPDATE pg_index SET indisvalid = false WHERE indexrelid = "
        f"""'"{T}"."CITES_graphid"'::regclass;""",
    )
    assert (await _graphid_indexes(db_pool))["CITES"] is False
    await bootstrap_tenant(db_pool, SPEC)
    assert (await _graphid_indexes(db_pool))["CITES"] is True


@requires_db
@pytest.mark.asyncio
async def test_no_label_table_is_left_without_statistics(db_pool: asyncpg.Pool) -> None:
    await register_tenant(db_pool, SPEC)
    never = await db_pool.fetch(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relkind = 'r' AND c.reltuples < 0;",
        T,
    )
    assert [r["relname"] for r in never] == []

"""AGE 1.8's cache-invalidation trigger on every label table, after a move from 1.6.

A dump of an AGE 1.6 database restored into AGE 1.8 brings label tables
without the ``_age_cache_invalidate`` trigger AGE 1.8 gives the ones it
creates. Both the compose's import (``deploy/db-upgrade/import-pg18.sh``) and
graphos's tenant bootstrap add it where missing. Removing the trigger stands
in for such a table.

DB-gated; the AGE 1.8 tests skip on AGE 1.6 (no such trigger function there),
where the last test checks that both are a no-op.
"""

from __future__ import annotations

import re
from pathlib import Path

import asyncpg
import pytest

from graphos.bootstrap import (
    CACHE_TRIGGER,
    TenantSpec,
    bootstrap_tenant,
    ensure_cache_invalidation_triggers,
    register_tenant,
)

from .conftest import requires_db

T = "graphos_test_age_cache_trigger"
SPEC = TenantSpec(
    name=T,
    vertex_labels=("Doc", "Person", "Author"),
    edge_labels=("WROTE", "CITES"),
    label_hierarchy=(("Author", "Person"),),
)
IMPORT_SCRIPT = Path(__file__).parents[1] / "deploy" / "db-upgrade" / "import-pg18.sh"


async def _has_trigger_function(pool: asyncpg.Pool) -> bool:
    return bool(await pool.fetchval(
        "SELECT to_regprocedure('ag_catalog.age_invalidate_graph_cache()') IS NOT NULL;",
    ))


async def _label_tables(conn: asyncpg.Connection | asyncpg.Pool) -> dict[str, tuple | None]:
    """label table -> its trigger's (type, function), or None without one."""
    rows = await conn.fetch(
        "SELECT c.relname, t.tgtype, t.tgfoid::regprocedure::text AS fn "
        "FROM ag_catalog.ag_label l "
        "JOIN ag_catalog.ag_graph g ON g.graphid = l.graph "
        "JOIN pg_class c ON c.oid = l.relation "
        "LEFT JOIN pg_trigger t ON t.tgrelid = c.oid AND t.tgname = $2 "
        "WHERE g.name = $1 AND l.name NOT IN ('_ag_label_vertex', '_ag_label_edge');",
        T, CACHE_TRIGGER,
    )
    return {r["relname"]: (r["tgtype"], r["fn"]) if r["fn"] else None for r in rows}


async def _as_restored_from_age_16(pool: asyncpg.Pool) -> tuple:
    """Register the tenant and take the trigger off its label tables.

    Returns the trigger AGE 1.8 itself puts on a new label table, to compare
    with what is added back.
    """
    await register_tenant(pool, SPEC)
    async with pool.acquire() as conn:
        # A label no registration declares: its table is one of the graph's too.
        if not await conn.fetchval(
            "SELECT 1 FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g "
            "ON g.graphid = l.graph WHERE g.name = $1 AND l.name = 'Loose';", T,
        ):
            await conn.execute(f"SELECT create_vlabel('{T}', 'Loose');")
        made_by_age = (await _label_tables(conn))["Loose"]
        for table in await _label_tables(conn):
            await conn.execute(f'DROP TRIGGER IF EXISTS {CACHE_TRIGGER} ON "{T}"."{table}";')
        assert set((await _label_tables(conn)).values()) == {None}
    assert made_by_age is not None
    return made_by_age


def _import_script_block() -> str:
    """The DO block import-pg18.sh runs to add the trigger."""
    blocks = re.findall(r"^DO \$\$.*?^END \$\$;", IMPORT_SCRIPT.read_text(), re.S | re.M)
    [block] = [b for b in blocks if CACHE_TRIGGER in b]
    return block


@requires_db
@pytest.mark.asyncio
async def test_bootstrap_adds_the_trigger_to_every_label_table(db_pool: asyncpg.Pool) -> None:
    if not await _has_trigger_function(db_pool):
        pytest.skip("AGE before 1.8: no cache-invalidation trigger")
    made_by_age = await _as_restored_from_age_16(db_pool)

    await bootstrap_tenant(db_pool, SPEC)    # what graphos-api runs at start

    tables = await _label_tables(db_pool)
    assert {"Doc", "Person", "Author", "WROTE", "CITES", "Loose"} <= set(tables)
    assert tables == dict.fromkeys(tables, made_by_age)
    async with db_pool.acquire() as conn:
        assert await ensure_cache_invalidation_triggers(conn, T) == 0


@requires_db
@pytest.mark.asyncio
async def test_the_import_script_adds_the_trigger_to_every_label_table(
    db_pool: asyncpg.Pool,
) -> None:
    if not await _has_trigger_function(db_pool):
        pytest.skip("AGE before 1.8: no cache-invalidation trigger")
    made_by_age = await _as_restored_from_age_16(db_pool)
    block = _import_script_block()

    async with db_pool.acquire() as conn:
        # The block covers every graph in the database; rolled back, so a shared
        # test database keeps its other graphs as they were.
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(block)
            tables = await _label_tables(conn)
            await conn.execute(block)            # a second run adds nothing
            assert await _label_tables(conn) == tables
        finally:
            await tx.rollback()
    assert tables == dict.fromkeys(tables, made_by_age)


@requires_db
@pytest.mark.asyncio
async def test_before_age_18_there_is_nothing_to_add(db_pool: asyncpg.Pool) -> None:
    if await _has_trigger_function(db_pool):
        pytest.skip("AGE 1.8 or later: covered above")
    await register_tenant(db_pool, SPEC)
    async with db_pool.acquire() as conn:
        assert await ensure_cache_invalidation_triggers(conn, T) == 0
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(_import_script_block())
        finally:
            await tx.rollback()
    assert set((await _label_tables(db_pool)).values()) == {None}

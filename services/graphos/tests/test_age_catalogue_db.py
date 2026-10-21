"""AGE's catalogue after a restore: graphids pointed back at their schemas.

A pg_dump restored into another database gives each graph's schema a new OID
but restores ``ag_graph.graphid`` (and ``ag_label.graph``) as they were, so
Cypher fails until they match again. Moving a graph's ids to another OID, in
one statement as a restore leaves them, stands in for that.

DB-gated.
"""

from __future__ import annotations

import os

import asyncpg
import pytest

from graphos.db import repair_age_catalogue

from .conftest import requires_db

G = "graphos_test_age_catalogue"
# An OID no schema has: where a restore leaves the ids pointing.
STALE_OID = 4_000_000_000

_MOVE_SQL = """
WITH moved AS (
  SELECT graphid AS old_id FROM ag_catalog.ag_graph WHERE name = $1
), graphs AS (
  UPDATE ag_catalog.ag_graph g SET graphid = $2::oid FROM moved m WHERE g.graphid = m.old_id
)
UPDATE ag_catalog.ag_label l SET graph = $2::oid FROM moved m WHERE l.graph = m.old_id;
"""


async def _count() -> int:
    """Cypher on a new connection, so no AGE cache from before is in the way."""
    conn = await asyncpg.connect(os.environ["GRAPHOS_TEST_DSN"])
    try:
        await conn.execute("LOAD 'age';")
        await conn.execute('SET search_path = ag_catalog, "$user", public;')
        value = await conn.fetchval(
            f"SELECT c::text FROM cypher('{G}', $$ MATCH (n:Doc) RETURN count(n) $$) "
            f"AS (c agtype);"
        )
        return int(value)
    finally:
        await conn.close()


async def _drop(conn: asyncpg.Connection) -> None:
    if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", G):
        await conn.execute(f"SELECT drop_graph('{G}', true);")


@requires_db
@pytest.mark.asyncio
async def test_a_restored_catalogue_is_pointed_back_at_its_schemas(db_pool: asyncpg.Pool) -> None:
    async with db_pool.acquire() as conn:
        await _drop(conn)
        await conn.execute(f"SELECT create_graph('{G}');")
        await conn.execute(f"SELECT create_vlabel('{G}', 'Doc');")
        await conn.execute(f'INSERT INTO "{G}"."Doc" (properties) VALUES (\'{{"id": "a"}}\');')
    try:
        assert await _count() == 1
        async with db_pool.acquire() as conn:
            await conn.execute(_MOVE_SQL, G, STALE_OID)
        with pytest.raises(asyncpg.PostgresError, match="does not exist"):
            await _count()

        assert G in await repair_age_catalogue(db_pool)

        async with db_pool.acquire() as conn:
            assert not await conn.fetchval(
                "SELECT count(*) FROM ag_catalog.ag_graph WHERE graphid <> namespace::oid;"
            )
        assert await _count() == 1
        assert await repair_age_catalogue(db_pool) == []   # nothing left to repair
    finally:
        await repair_age_catalogue(db_pool)
        async with db_pool.acquire() as conn:
            await _drop(conn)

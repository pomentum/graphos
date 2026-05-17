"""End-to-end test for the search vertical.

DB-touching: requires a running graphos-db with AGE loaded. Gated on
``GRAPHOS_TEST_DSN``. Exercises the full path:

    1. Register a tenant via register_tenant() (idempotent).
    2. Insert a handful of nodes via raw cypher() into the AGE graph.
    3. Run a substring search through query_cypher() and assert results.

This test stays minimal — it's the contract for "the AGE-cypher-asyncpg
pattern works end-to-end". Higher-coverage integration tests for the
HTTP endpoint shape live in a future TestClient + httpx-mock pass.
"""

from __future__ import annotations

import asyncpg
import pytest

from graphos.bootstrap import TenantSpec, register_tenant
from graphos.cypher import query_cypher

from .conftest import requires_db

TEST_TENANT = "graphos_test_search"


@requires_db
@pytest.mark.asyncio
async def test_search_finds_nodes_by_name(db_pool: asyncpg.Pool) -> None:
    """Register a tenant, insert nodes, search by substring."""
    spec = TenantSpec(
        name=TEST_TENANT,
        vertex_labels=("Thing",),
    )
    await register_tenant(db_pool, spec)

    # Insert three Thing nodes — one matching, two not.
    async with db_pool.acquire() as conn:
        await conn.execute(
            f"""
            SELECT * FROM cypher('{TEST_TENANT}', $$
              CREATE (:Thing {{name: 'foobar', kind: 'A'}})
              CREATE (:Thing {{name: 'foobaz', kind: 'B'}})
              CREATE (:Thing {{name: 'unrelated', kind: 'C'}})
            $$) AS (v agtype);
            """
        )

    # Substring 'foob' should match the first two.
    rows = await query_cypher(
        db_pool,
        graph=TEST_TENANT,
        cypher="""
            MATCH (n)
            WHERE n.name IS NOT NULL AND n.name CONTAINS $q
            RETURN id(n) AS id, label(n) AS lbl, properties(n) AS props
            LIMIT $lim
        """,
        return_cols=[("id", "agtype"), ("lbl", "agtype"), ("props", "agtype")],
        params={"q": "foob", "lim": 10},
    )

    names = sorted(r["props"]["name"] for r in rows)
    assert names == ["foobar", "foobaz"]

    # Every row must have an id, a label, and a properties dict.
    for r in rows:
        assert isinstance(r["id"], int)
        assert r["lbl"] == "Thing"
        assert "name" in r["props"]

    # Tighter substring: 'foobar' only.
    rows = await query_cypher(
        db_pool,
        graph=TEST_TENANT,
        cypher="""
            MATCH (n)
            WHERE n.name IS NOT NULL AND n.name CONTAINS $q
            RETURN id(n) AS id, label(n) AS lbl, properties(n) AS props
            LIMIT $lim
        """,
        return_cols=[("id", "agtype"), ("lbl", "agtype"), ("props", "agtype")],
        params={"q": "foobar", "lim": 10},
    )
    assert len(rows) == 1
    assert rows[0]["props"]["name"] == "foobar"

    # Cleanup: drop the test graph so re-runs are deterministic.
    async with db_pool.acquire() as conn:
        await conn.execute(f"SELECT drop_graph('{TEST_TENANT}', true);")

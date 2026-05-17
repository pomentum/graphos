"""End-to-end test for the node-upsert vertical.

DB-touching, gated on GRAPHOS_TEST_DSN. Exercises:

    1. Register a tenant.
    2. UNWIND-MERGE a small batch via raw cypher().
    3. Re-MERGE the same batch (idempotent — total node count unchanged).
    4. MERGE with new + overlapping ids; verify additive props merge.
"""

from __future__ import annotations

import asyncpg
import pytest

from graphos.bootstrap import TenantSpec, register_tenant
from graphos.cypher import (
    query_cypher,
    upsert_edges_inline,
    upsert_nodes_inline,
)

from .conftest import requires_db

TEST_TENANT = "graphos_test_upsert"
TEST_TENANT_EDGES = "graphos_test_edges"


async def _count_nodes(pool: asyncpg.Pool, label: str) -> int:
    rows = await query_cypher(
        pool,
        graph=TEST_TENANT,
        cypher=f"MATCH (n:{label}) RETURN count(n) AS c",
        return_cols=[("c", "agtype")],
    )
    return int(rows[0]["c"]) if rows else 0


async def _props_by_id(pool: asyncpg.Pool, label: str, node_id: str) -> dict:
    rows = await query_cypher(
        pool,
        graph=TEST_TENANT,
        cypher=f"MATCH (n:{label} {{id: $id}}) RETURN properties(n) AS p",
        return_cols=[("p", "agtype")],
        params={"id": node_id},
    )
    return dict(rows[0]["p"]) if rows else {}


@requires_db
@pytest.mark.asyncio
async def test_upsert_is_idempotent_and_additive(db_pool: asyncpg.Pool) -> None:
    spec = TenantSpec(name=TEST_TENANT, vertex_labels=("Thing",))
    await register_tenant(db_pool, spec)

    rows_v1 = [
        {"id": "a", "props": {"name": "alpha", "kind": "x"}},
        {"id": "b", "props": {"name": "beta"}},
    ]

    # 1st upsert
    await upsert_nodes_inline(db_pool, graph=TEST_TENANT, label="Thing", rows=rows_v1)
    assert await _count_nodes(db_pool, "Thing") == 2

    # 2nd upsert: same payload → still 2 nodes
    await upsert_nodes_inline(db_pool, graph=TEST_TENANT, label="Thing", rows=rows_v1)
    assert await _count_nodes(db_pool, "Thing") == 2

    # 3rd upsert: add a new property to 'a', and a new node 'c'.
    rows_v2 = [
        {"id": "a", "props": {"extra": 99}},                        # adds key
        {"id": "c", "props": {"name": "gamma"}},                    # new node
    ]
    await upsert_nodes_inline(db_pool, graph=TEST_TENANT, label="Thing", rows=rows_v2)
    assert await _count_nodes(db_pool, "Thing") == 3

    # 'a' should keep its original 'name' and 'kind', plus the new 'extra'.
    props_a = await _props_by_id(db_pool, "Thing", "a")
    assert props_a["id"] == "a"
    assert props_a["name"] == "alpha"
    assert props_a["kind"] == "x"
    assert props_a["extra"] == 99

    # Cleanup
    async with db_pool.acquire() as conn:
        await conn.execute(f"SELECT drop_graph('{TEST_TENANT}', true);")


@requires_db
@pytest.mark.asyncio
async def test_edge_upsert_skips_missing_endpoints(db_pool: asyncpg.Pool) -> None:
    """Upsert idempotency for edges + skip-on-missing-endpoint behaviour."""
    spec = TenantSpec(
        name=TEST_TENANT_EDGES,
        vertex_labels=("Document", "Collection"),
        edge_labels=("CITES", "PART_OF"),
    )
    await register_tenant(db_pool, spec)

    # Seed two Documents and one Collection.
    await upsert_nodes_inline(
        db_pool, graph=TEST_TENANT_EDGES, label="Document",
        rows=[{"id": "a", "props": {"name": "alpha"}},
              {"id": "b", "props": {"name": "beta"}}],
    )
    await upsert_nodes_inline(
        db_pool, graph=TEST_TENANT_EDGES, label="Collection",
        rows=[{"id": "cmp", "props": {"name": "auth"}}],
    )

    # 1. Edge whose endpoints both exist → merged.
    # 2. Edge whose dst is missing → skipped.
    merged, skipped = await upsert_edges_inline(
        db_pool,
        graph=TEST_TENANT_EDGES,
        src_label="Document", dst_label="Document", edge_type="CITES",
        rows=[
            {"src_id": "a", "dst_id": "b", "props": {"weight": 0.8}},
            {"src_id": "a", "dst_id": "missing", "props": {"weight": 0.1}},
        ],
    )
    assert merged == 1
    assert skipped == 1

    # Idempotent re-merge: same outcome, no new edges.
    merged2, skipped2 = await upsert_edges_inline(
        db_pool,
        graph=TEST_TENANT_EDGES,
        src_label="Document", dst_label="Document", edge_type="CITES",
        rows=[{"src_id": "a", "dst_id": "b", "props": {"weight": 0.8}}],
    )
    assert merged2 == 1
    assert skipped2 == 0

    # Confirm exactly one CITES edge exists.
    rows = await query_cypher(
        db_pool,
        graph=TEST_TENANT_EDGES,
        cypher="MATCH ()-[r:CITES]->() RETURN count(r) AS c",
        return_cols=[("c", "agtype")],
    )
    assert int(rows[0]["c"]) == 1

    # Additive props merge: re-upsert with new prop, weight preserved.
    await upsert_edges_inline(
        db_pool,
        graph=TEST_TENANT_EDGES,
        src_label="Document", dst_label="Document", edge_type="CITES",
        rows=[{"src_id": "a", "dst_id": "b", "props": {"confidence": 0.95}}],
    )
    rows = await query_cypher(
        db_pool,
        graph=TEST_TENANT_EDGES,
        cypher=(
            "MATCH (:Document {id:'a'})-[r:CITES]->(:Document {id:'b'}) "
            "RETURN properties(r) AS p"
        ),
        return_cols=[("p", "agtype")],
    )
    props = dict(rows[0]["p"])
    assert props["weight"] == 0.8
    assert props["confidence"] == 0.95

    # Cross-label edge (Document → Collection, PART_OF).
    merged3, skipped3 = await upsert_edges_inline(
        db_pool,
        graph=TEST_TENANT_EDGES,
        src_label="Document", dst_label="Collection", edge_type="PART_OF",
        rows=[{"src_id": "a", "dst_id": "cmp", "props": {}}],
    )
    assert merged3 == 1
    assert skipped3 == 0

    # Cleanup
    async with db_pool.acquire() as conn:
        await conn.execute(f"SELECT drop_graph('{TEST_TENANT_EDGES}', true);")

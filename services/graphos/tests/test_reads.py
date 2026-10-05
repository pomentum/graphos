"""End-to-end tests for the read endpoints (neighbors, induced_edges).

DB-gated. Seed a small graph then exercise the Cypher built by each
handler via the helper functions they delegate to.
"""

from __future__ import annotations

import asyncpg
import pytest

from graphos import writes
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.cypher import query_cypher

from .conftest import requires_db

TEST_TENANT = "graphos_test_reads"


async def _seed(pool: asyncpg.Pool) -> None:
    spec = TenantSpec(
        name=TEST_TENANT,
        vertex_labels=("Document", "Collection"),
        edge_labels=("CITES", "PART_OF"),
    )
    await register_tenant(pool, spec)

    # 4 Documents (a, b, c, d) + 1 Collection (cmp)
    await writes.upsert_nodes(
        pool, tenant=TEST_TENANT, label="Document",
        rows=[
            {"id": "a", "props": {"name": "alpha"}},
            {"id": "b", "props": {"name": "beta"}},
            {"id": "c", "props": {"name": "gamma"}},
            {"id": "d", "props": {"name": "delta"}},
        ],
    )
    await writes.upsert_nodes(
        pool, tenant=TEST_TENANT, label="Collection",
        rows=[{"id": "cmp", "props": {"name": "auth"}}],
    )

    # Edges: a-CITES->b, a-CITES->c, b-CITES->c, a-PART_OF->cmp
    await writes.upsert_edges(
        pool, tenant=TEST_TENANT,
        src_label="Document", dst_label="Document", edge_type="CITES",
        rows=[
            {"src_id": "a", "dst_id": "b", "props": {"w": 0.9}},
            {"src_id": "a", "dst_id": "c", "props": {"w": 0.5}},
            {"src_id": "b", "dst_id": "c", "props": {"w": 0.3}},
        ],
    )
    await writes.upsert_edges(
        pool, tenant=TEST_TENANT,
        src_label="Document", dst_label="Collection", edge_type="PART_OF",
        rows=[{"src_id": "a", "dst_id": "cmp", "props": {}}],
    )


@requires_db
@pytest.mark.asyncio
async def test_neighbors_returns_in_and_out(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)

    # Around 'a': 3 outs (CITES:b, CITES:c, PART_OF:cmp), 0 ins.
    out_q = """
        MATCH (n {id: $node_id})-[r]->(m)
        RETURN m.id AS nid, type(r) AS rt LIMIT $lim
    """
    in_q = """
        MATCH (m)-[r]->(n {id: $node_id})
        RETURN m.id AS nid, type(r) AS rt LIMIT $lim
    """
    cols = [("nid", "agtype"), ("rt", "agtype")]

    outs = await query_cypher(db_pool, graph=TEST_TENANT, cypher=out_q,
                              return_cols=cols, params={"node_id": "a", "lim": 50})
    ins = await query_cypher(db_pool, graph=TEST_TENANT, cypher=in_q,
                             return_cols=cols, params={"node_id": "a", "lim": 50})
    out_pairs = sorted((r["nid"], r["rt"]) for r in outs)
    assert out_pairs == [("b", "CITES"), ("c", "CITES"), ("cmp", "PART_OF")]
    assert ins == []

    # Around 'c': 0 outs, 2 ins (a, b).
    outs = await query_cypher(db_pool, graph=TEST_TENANT, cypher=out_q,
                              return_cols=cols, params={"node_id": "c", "lim": 50})
    ins = await query_cypher(db_pool, graph=TEST_TENANT, cypher=in_q,
                             return_cols=cols, params={"node_id": "c", "lim": 50})
    assert outs == []
    in_ids = sorted(r["nid"] for r in ins)
    assert in_ids == ["a", "b"]


@requires_db
@pytest.mark.asyncio
async def test_induced_edges(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)

    # Induce over {a, b, c}: should return a→b, a→c, b→c (3 edges).
    # 'd' has no edges in this set; 'cmp' isn't in the set.
    cypher = """
        MATCH (a)-[r]->(b)
        WHERE a.id IN $ids AND b.id IN $ids
        RETURN a.id AS src, b.id AS dst, type(r) AS rt
        LIMIT $lim
    """
    rows = await query_cypher(
        db_pool, graph=TEST_TENANT,
        cypher=cypher,
        return_cols=[("src", "agtype"), ("dst", "agtype"), ("rt", "agtype")],
        params={"ids": ["a", "b", "c"], "lim": 100},
    )
    edges = sorted((r["src"], r["dst"], r["rt"]) for r in rows)
    assert edges == [
        ("a", "b", "CITES"),
        ("a", "c", "CITES"),
        ("b", "c", "CITES"),
    ]

    # Restrict to ids that contain the Collection endpoint:
    rows = await query_cypher(
        db_pool, graph=TEST_TENANT,
        cypher=cypher,
        return_cols=[("src", "agtype"), ("dst", "agtype"), ("rt", "agtype")],
        params={"ids": ["a", "cmp"], "lim": 100},
    )
    edges = sorted((r["src"], r["dst"], r["rt"]) for r in rows)
    assert edges == [("a", "cmp", "PART_OF")]

    # Cleanup
    async with db_pool.acquire() as conn:
        await conn.execute(f"SELECT drop_graph('{TEST_TENANT}', true);")

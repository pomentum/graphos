"""End-to-end tests for POST /edges:scan paging.

DB-gated. The handler is called directly with a real pool. Results are
checked against the reference the endpoint has always implemented: every
edge of the type whose two endpoint vertices exist, in edge graphid
order. That includes edges left dangling by a vertex removed without
DETACH, which must be skipped without shortening a page.
"""

from __future__ import annotations

from types import SimpleNamespace

import asyncpg
import pytest

from graphos.api.graph import scan_edges
from graphos.auth import TokenPrincipal
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.contracts.graph import EdgesScanRequest, NodesScanWhere
from graphos.cypher import upsert_edges_inline, upsert_nodes_inline

from .conftest import requires_db

TEST_TENANT = "graphos_test_scan_edges"
N_DOCS = 12
ADMIN = TokenPrincipal(consumer="test", tenants=frozenset(), admin=True)


async def _seed(pool: asyncpg.Pool) -> None:
    await register_tenant(pool, TenantSpec(
        name=TEST_TENANT,
        vertex_labels=("Document", "Collection"),
        edge_labels=("CITES", "PART_OF"),
    ))
    async with pool.acquire() as conn:
        for t in ("CITES", "PART_OF", "Document", "Collection"):
            await conn.execute(f'DELETE FROM "{TEST_TENANT}"."{t}";')
    await upsert_nodes_inline(
        pool, graph=TEST_TENANT, label="Document",
        rows=[{"id": f"d{i}", "props": {"name": f"doc {i}"}} for i in range(N_DOCS)],
    )
    await upsert_nodes_inline(
        pool, graph=TEST_TENANT, label="Collection",
        rows=[{"id": "c0", "props": {"name": "shelf"}}],
    )
    # Every ordered pair i -> i+1 and i -> i+2, with a property to filter on.
    rows = [
        {"src_id": f"d{i}", "dst_id": f"d{j}", "props": {"hop": j - i}}
        for i in range(N_DOCS) for j in (i + 1, i + 2) if j < N_DOCS
    ]
    await upsert_edges_inline(
        pool, graph=TEST_TENANT, src_label="Document", dst_label="Document",
        edge_type="CITES", rows=rows,
    )
    await upsert_edges_inline(
        pool, graph=TEST_TENANT, src_label="Document", dst_label="Collection",
        edge_type="PART_OF",
        rows=[{"src_id": f"d{i}", "dst_id": "c0", "props": {}} for i in range(N_DOCS)],
    )
    # Remove two documents WITHOUT detaching: their edges stay behind,
    # dangling, spread through the edge order.
    async with pool.acquire() as conn:
        await conn.execute(
            f'DELETE FROM "{TEST_TENANT}"."Document" '
            "WHERE properties -> '\"id\"'::agtype IN ('\"d3\"'::agtype, '\"d7\"'::agtype);"
        )


async def _reference(pool: asyncpg.Pool, edge_type: str, hop: int | None = None) -> list[tuple]:
    """The endpoint's semantics, as one flat inner-join query."""
    where = f"WHERE e.properties -> '\"hop\"'::agtype = '{hop}'::agtype" if hop else ""
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT trim(both '"' from (vs.properties -> '"id"'::agtype)::text) AS s,
                   cs.relname AS sl,
                   trim(both '"' from (vd.properties -> '"id"'::agtype)::text) AS d,
                   cd.relname AS dl
            FROM "{TEST_TENANT}"."{edge_type}" e
            JOIN "{TEST_TENANT}"."_ag_label_vertex" vs ON vs.id = e.start_id
            JOIN "{TEST_TENANT}"."_ag_label_vertex" vd ON vd.id = e.end_id
            JOIN pg_class cs ON cs.oid = vs.tableoid
            JOIN pg_class cd ON cd.oid = vd.tableoid
            {where}
            ORDER BY e.id;""")
    return [(edge_type, r["s"], r["sl"], r["d"], r["dl"]) for r in rows]


async def _scan_all(pool: asyncpg.Pool, **body) -> tuple[list[tuple], list[int]]:
    """Follow next_cursor to the end; return the items and each page size."""
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
    items: list[tuple] = []
    sizes: list[int] = []
    cursor = None
    for _ in range(100):
        resp = await scan_edges(
            TEST_TENANT, EdgesScanRequest(cursor=cursor, **body), request, ADMIN,
        )
        items += [(i.type, i.src_id, i.src_label, i.dst_id, i.dst_label) for i in resp.items]
        sizes.append(len(resp.items))
        cursor = resp.next_cursor
        if cursor is None:
            return items, sizes
    raise AssertionError("scan did not terminate")


@requires_db
@pytest.mark.asyncio
async def test_scan_edges_pages_match_reference(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    expected = await _reference(db_pool, "CITES")
    assert expected, "seed produced no live edges"
    for limit in (1, 3, 7, 1000):
        items, sizes = await _scan_all(db_pool, types=["CITES"], limit=limit)
        assert items == expected, f"limit={limit}"
        # Dangling edges never shorten a page: every page but the last is full.
        assert all(s == limit for s in sizes[:-1]), (limit, sizes)


@requires_db
@pytest.mark.asyncio
async def test_scan_edges_skips_dangling_edges(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    items, _ = await _scan_all(db_pool, types=["CITES"], limit=5)
    ids = {x[1] for x in items} | {x[3] for x in items}
    assert "d3" not in ids and "d7" not in ids
    async with db_pool.acquire() as conn:
        total = await conn.fetchval(f'SELECT count(*) FROM "{TEST_TENANT}"."CITES";')
    assert total > len(items), "seed should have left dangling edges behind"


@requires_db
@pytest.mark.asyncio
async def test_scan_edges_where_and_multiple_types(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    hop2 = await _reference(db_pool, "CITES", hop=2)
    items, _ = await _scan_all(
        db_pool, types=["CITES"], limit=2,
        where=[NodesScanWhere(property="hop", op="=", value=2)],
    )
    assert items == hop2

    both = await _reference(db_pool, "CITES") + await _reference(db_pool, "PART_OF")
    items, _ = await _scan_all(db_pool, types=["CITES", "PART_OF"], limit=4)
    assert items == both

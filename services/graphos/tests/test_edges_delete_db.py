"""DB tests for the batch edge delete: ``writes.delete_edges`` and
``POST /v1/{tenant}/graph/edges:delete``.

DB-gated.  The tenant has the id ``shared`` in two labels, with a ``LINKS_TO``
edge from ``a`` to each, so naming that edge by its ids alone matches two
edges: the batch is refused, and naming the label picks one.  The API is
driven in-process through httpx's ASGI transport, with the test pool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import changes, writes
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_edges_delete"
QUOTED = 'q"uote\\d'   # an id that naive string quoting into agtype would break


async def _seed(pool: asyncpg.Pool) -> None:
    await register_tenant(pool, TenantSpec(
        name=TENANT, vertex_labels=("Document", "Collection"),
        edge_labels=("CITES", "PART_OF", "LINKS_TO"),
    ))
    async with pool.acquire() as conn:
        for t in ("CITES", "PART_OF", "LINKS_TO", "Document", "Collection"):
            await conn.execute(f'DELETE FROM "{TENANT}"."{t}";')
        await conn.execute("DELETE FROM graphos_meta.changes WHERE tenant = $1;", TENANT)
    await writes.upsert_nodes(pool, tenant=TENANT, label="Document", rows=[
        {"id": i, "props": {}} for i in ("a", "b", "c", "shared", QUOTED)
    ])
    await writes.upsert_nodes(pool, tenant=TENANT, label="Collection", rows=[
        {"id": "shared", "props": {}}, {"id": "box", "props": {}},
    ])
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Document", dst_label="Document", edge_type="CITES",
        rows=[{"src_id": "a", "dst_id": "b", "props": {}},
              {"src_id": "b", "dst_id": "c", "props": {}},
              {"src_id": "c", "dst_id": QUOTED, "props": {}}],
    )
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Document", dst_label="Collection", edge_type="PART_OF",
        rows=[{"src_id": "a", "dst_id": "box", "props": {}}],
    )
    for dst_label in ("Document", "Collection"):
        await writes.upsert_edges(
            pool, tenant=TENANT, src_label="Document", dst_label=dst_label, edge_type="LINKS_TO",
            rows=[{"src_id": "a", "dst_id": "shared", "props": {}}],
        )
    async with pool.acquire() as conn:
        # Only the deletes under test are in the feed.
        await conn.execute("DELETE FROM graphos_meta.changes WHERE tenant = $1;", TENANT)


async def _edges(pool: asyncpg.Pool) -> set[tuple[str, str, str]]:
    rows = await pool.fetch(
        f"SELECT c.relname AS type, s.properties::text::jsonb ->> 'id' AS src, "
        f"       d.properties::text::jsonb ->> 'id' AS dst "
        f'FROM "{TENANT}"."_ag_label_edge" e JOIN pg_class c ON c.oid = e.tableoid '
        f'JOIN "{TENANT}"."_ag_label_vertex" s ON s.id = e.start_id '
        f'JOIN "{TENANT}"."_ag_label_vertex" d ON d.id = e.end_id;'
    )
    return {(r["type"], r["src"], r["dst"]) for r in rows}


async def _node_count(pool: asyncpg.Pool) -> int:
    return int(await pool.fetchval(f'SELECT count(*) FROM "{TENANT}"."_ag_label_vertex";'))


async def _deletes(pool: asyncpg.Pool) -> list[tuple[Any, ...]]:
    async with pool.acquire() as conn:
        page = await changes.read(conn, TENANT, None, 1000)
    return [(c.label, c.src_label, c.dst_label, c.ids, c.count)
            for c in page.changes if c.op == "delete"]


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _seed(db_pool)
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://test/v1/{TENANT}/graph",
    ) as client:
        yield client


# ── writes ─────────────────────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_deletes_the_named_edges_only_and_their_ends_stay(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    before, nodes = await _edges(db_pool), await _node_count(db_pool)
    edges: list[writes.EdgeKey] = [
        ("CITES", "a", "b", None, None),
        ("CITES", "c", QUOTED, None, None),
        ("PART_OF", "a", "box", None, None),
        ("CITES", "a", "b", None, None),        # named twice: skipped
        ("CITES", "b", "a", None, None),        # the other direction: no such edge
        ("PART_OF", "a", "nope", None, None),   # no such end
    ]

    assert await writes.delete_edges(db_pool, tenant=TENANT, edges=edges) == 3
    assert await _edges(db_pool) == before - {
        ("CITES", "a", "b"), ("CITES", "c", QUOTED), ("PART_OF", "a", "box"),
    }
    assert await _node_count(db_pool) == nodes
    # Idempotent.
    assert await writes.delete_edges(db_pool, tenant=TENANT, edges=edges) == 0


@requires_db
@pytest.mark.asyncio
async def test_records_one_change_per_type_and_label_pair(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    await writes.delete_edges(db_pool, tenant=TENANT, edges=[
        ("PART_OF", "a", "box", None, None),
        ("CITES", "b", "c", None, None),
        ("CITES", "a", "b", None, None),
        ("LINKS_TO", "a", "shared", None, "Collection"),
        ("CITES", "c", "nope", None, None),
    ])
    assert await _deletes(db_pool) == [
        ("CITES", "Document", "Document", [["b", "c"], ["a", "b"]], 2),
        ("LINKS_TO", "Document", "Collection", [["a", "shared"]], 1),
        ("PART_OF", "Document", "Collection", [["a", "box"]], 1),
    ]
    await writes.delete_edges(db_pool, tenant=TENANT, edges=[("CITES", "a", "b", None, None)])
    assert len(await _deletes(db_pool)) == 3


@requires_db
@pytest.mark.asyncio
async def test_an_ambiguous_edge_deletes_nothing_and_a_label_picks_one(
    db_pool: asyncpg.Pool,
) -> None:
    await _seed(db_pool)
    before = await _edges(db_pool)
    with pytest.raises(writes.AmbiguousEdgeError) as caught:
        await writes.delete_edges(db_pool, tenant=TENANT, edges=[
            ("CITES", "a", "b", None, None),            # comes first, and is rolled back
            ("LINKS_TO", "a", "shared", None, None),
        ])
    assert caught.value.edge == ("LINKS_TO", "a", "shared")
    assert sorted(caught.value.candidates) == ["Document->Collection", "Document->Document"]
    assert await _edges(db_pool) == before
    assert await _deletes(db_pool) == []

    assert await writes.delete_edges(db_pool, tenant=TENANT, edges=[
        ("LINKS_TO", "a", "shared", None, "Document"),
    ]) == 1
    remaining = await db_pool.fetchval(f'SELECT count(*) FROM "{TENANT}"."LINKS_TO";')
    assert remaining == 1


# ── API ────────────────────────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_post_edges_delete(api: httpx.AsyncClient, db_pool: asyncpg.Pool) -> None:
    body = {"edges": [
        {"type": "CITES", "src_id": "b", "dst_id": "c"},
        {"type": "CITES", "src_id": "c", "dst_id": QUOTED},
        {"type": "CITES", "src_id": "b", "dst_id": "c"},
        {"type": "PART_OF", "src_id": "a", "dst_id": "nope"},
    ]}
    resp = await api.post("/edges:delete", json=body)
    assert resp.status_code == HTTPStatus.OK, resp.text
    got = resp.json()
    assert (got["tenant"], got["edges_requested"], got["edges_deleted"]) == (TENANT, 4, 2)
    assert isinstance(got["elapsed_ms"], int)
    assert ("CITES", "b", "c") not in await _edges(db_pool)

    again = (await api.post("/edges:delete", json=body)).json()
    assert again["edges_deleted"] == 0


@requires_db
@pytest.mark.asyncio
async def test_post_edges_delete_refuses_what_it_cannot_delete(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    before = await _edges(db_pool)
    resp = await api.post("/edges:delete", json={"edges": [
        {"type": "CITES", "src_id": "a", "dst_id": "b"},
        {"type": "WROTE", "src_id": "a", "dst_id": "b"},
    ]})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "WROTE" in resp.json()["detail"]

    resp = await api.post("/edges:delete", json={"edges": [
        {"type": "CITES", "src_id": "a", "dst_id": "b"},
        {"type": "LINKS_TO", "src_id": "a", "dst_id": "shared"},
    ]})
    assert resp.status_code == HTTPStatus.CONFLICT
    assert "from 'a' to 'shared'" in resp.json()["detail"]
    assert await _edges(db_pool) == before

    resp = await api.post("/edges:delete", json={"edges": []})
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

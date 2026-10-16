"""DB tests: ``POST /graph/edges:check`` answers for many edges at once.

DB-gated.  The tenant has the id ``shared`` in two labels, so an edge to it
named by id alone is ambiguous and must say so rather than pick one.  The
API is driven in-process through httpx's ASGI transport with the test pool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import writes
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_edges_check"


async def _seed(pool: asyncpg.Pool) -> None:
    await register_tenant(pool, TenantSpec(
        name=TENANT, vertex_labels=("Document", "Collection"), edge_labels=("CITES", "PART_OF"),
    ))
    async with pool.acquire() as conn:
        for t in ("CITES", "PART_OF", "Document", "Collection"):
            await conn.execute(f'DELETE FROM "{TENANT}"."{t}";')
    await writes.upsert_nodes(pool, tenant=TENANT, label="Document", rows=[
        {"id": "a", "props": {}}, {"id": "b", "props": {}}, {"id": "shared", "props": {}},
    ])
    await writes.upsert_nodes(pool, tenant=TENANT, label="Collection", rows=[
        {"id": "shared", "props": {}},
    ])
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Document", dst_label="Document", edge_type="CITES",
        rows=[{"src_id": "a", "dst_id": "b", "props": {"w": 1}},
              {"src_id": "a", "dst_id": "shared", "props": {}}],
    )
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Document", dst_label="Collection", edge_type="CITES",
        rows=[{"src_id": "a", "dst_id": "shared", "props": {"w": 2}}],
    )


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


@requires_db
@pytest.mark.asyncio
async def test_each_edge_is_answered_in_request_order(api: httpx.AsyncClient) -> None:
    r = await api.post("/edges:check", json={"edges": [
        {"type": "CITES", "src_id": "a", "dst_id": "b"},
        {"type": "CITES", "src_id": "b", "dst_id": "a"},
        {"type": "PART_OF", "src_id": "a", "dst_id": "b"},
        {"type": "CITES", "src_id": "a", "dst_id": "nowhere"},
    ]})
    assert r.status_code == HTTPStatus.OK, r.text
    body = r.json()
    assert [x["status"] for x in body["results"]] == ["present", "absent", "absent", "absent"]
    first = body["results"][0]
    assert (first["src_label"], first["dst_label"], first["rev"]) == ("Document", "Document", 1)
    assert first["etag"].startswith('"') and first["properties"] is None
    assert (body["present"], body["absent"], body["ambiguous"]) == (1, 3, 0)


@requires_db
@pytest.mark.asyncio
async def test_an_id_in_two_labels_is_ambiguous_until_a_label_picks_one(
    api: httpx.AsyncClient,
) -> None:
    r = await api.post("/edges:check", json={"include_properties": True, "edges": [
        {"type": "CITES", "src_id": "a", "dst_id": "shared"},
        {"type": "CITES", "src_id": "a", "dst_id": "shared", "dst_label": "Collection"},
    ]})
    assert r.status_code == HTTPStatus.OK, r.text
    ambiguous, picked = r.json()["results"]
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["candidates"] == ["Document->Collection", "Document->Document"]
    assert picked["status"] == "present"
    assert picked["properties"] == {"w": 2}


@requires_db
@pytest.mark.asyncio
async def test_an_unregistered_type_or_label_refuses_the_whole_request(
    api: httpx.AsyncClient,
) -> None:
    r = await api.post("/edges:check", json={"edges": [
        {"type": "CITES", "src_id": "a", "dst_id": "b"},
        {"type": "LINKS_TO", "src_id": "a", "dst_id": "b"},
    ]})
    assert r.status_code == HTTPStatus.BAD_REQUEST
    assert "LINKS_TO" in r.json()["detail"]
    r = await api.post("/edges:check", json={"edges": [
        {"type": "CITES", "src_id": "a", "dst_id": "b", "src_label": "Person"},
    ]})
    assert r.status_code == HTTPStatus.BAD_REQUEST


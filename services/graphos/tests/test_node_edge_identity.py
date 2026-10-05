"""DB tests: one node or one edge by identity (get, merge, patch, delete).

DB-gated.  The tenant has the id ``shared`` in two labels, so a request by
id alone is ambiguous there and must be refused rather than land on an
arbitrary row (or on both).  The API is driven in-process through httpx's
ASGI transport on the test's own event loop, with the test pool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import lookup, writes
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_identity"
QUOTED = 'q"uote\\d'   # an id that naive string quoting into agtype would break


async def _seed(pool: asyncpg.Pool) -> None:
    await register_tenant(pool, TenantSpec(
        name=TENANT, vertex_labels=("Document", "Collection"), edge_labels=("CITES", "PART_OF"),
    ))
    async with pool.acquire() as conn:
        for t in ("CITES", "PART_OF", "Document", "Collection"):
            await conn.execute(f'DELETE FROM "{TENANT}"."{t}";')
    await writes.upsert_nodes(pool, tenant=TENANT, label="Document", rows=[
        {"id": "a", "props": {"n": 1, "meta": {"x": 1, "y": 2}, "tags": ["p"]}},
        {"id": "shared", "props": {"kind": "doc"}},
        {"id": QUOTED, "props": {"k": "v"}},
    ])
    await writes.upsert_nodes(pool, tenant=TENANT, label="Collection", rows=[
        {"id": "shared", "props": {"kind": "coll"}},
    ])
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Document", dst_label="Document", edge_type="CITES",
        rows=[{"src_id": "a", "dst_id": "shared", "props": {"w": 1, "note": "d"}},
              {"src_id": "a", "dst_id": QUOTED, "props": {}}],
    )
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Document", dst_label="Collection", edge_type="CITES",
        rows=[{"src_id": "a", "dst_id": "shared", "props": {"w": 2}}],
    )


async def _node(pool: asyncpg.Pool, external_id: str, label: str) -> dict:
    async with pool.acquire() as conn:
        node = await lookup.find_node(conn, tenant=TENANT, external_id=external_id, label=label)
    assert node is not None
    return node.properties


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


# ── writes + lookup ────────────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_merge_refuses_an_id_in_two_labels(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    with pytest.raises(lookup.AmbiguousMatchError) as exc:
        await writes.merge_node_properties(
            db_pool, tenant=TENANT, external_id="shared", properties={"x": 1},
        )
    assert exc.value.candidates == ["Collection", "Document"]
    assert await _node(db_pool, "shared", "Document") == {"id": "shared", "kind": "doc"}
    assert await _node(db_pool, "shared", "Collection") == {"id": "shared", "kind": "coll"}

    node = await writes.merge_node_properties(
        db_pool, tenant=TENANT, external_id="shared", properties={"x": 1}, label="Collection",
    )
    assert node is not None
    assert node.label == "Collection"
    assert await _node(db_pool, "shared", "Collection") == {"id": "shared", "kind": "coll", "x": 1}
    assert await _node(db_pool, "shared", "Document") == {"id": "shared", "kind": "doc"}


@requires_db
@pytest.mark.asyncio
async def test_patch_node_is_a_json_merge_patch(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    node = await writes.patch_node(
        db_pool, tenant=TENANT, external_id="a",
        patch={"n": None, "meta": {"y": None, "z": 3}, "tags": ["q"], "new": True},
    )
    expected = {"id": "a", "meta": {"x": 1, "z": 3}, "tags": ["q"], "new": True}
    assert node is not None
    assert (node.label, node.properties) == ("Document", expected)
    assert await _node(db_pool, "a", "Document") == expected
    with pytest.raises(ValueError, match="'id'"):
        await writes.patch_node(db_pool, tenant=TENANT, external_id="a", patch={"id": "b"})
    assert await writes.patch_node(
        db_pool, tenant=TENANT, external_id="nope", patch={"x": 1},
    ) is None


@requires_db
@pytest.mark.asyncio
async def test_ids_with_quotes_are_matched_exactly(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    assert await _node(db_pool, QUOTED, "Document") == {"id": QUOTED, "k": "v"}
    node = await writes.merge_node_properties(
        db_pool, tenant=TENANT, external_id=QUOTED, properties={"k": "w"},
    )
    assert node is not None
    assert node.label == "Document"
    assert await _node(db_pool, QUOTED, "Document") == {"id": QUOTED, "k": "w"}


@requires_db
@pytest.mark.asyncio
async def test_edge_patch_and_delete_one_edge(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    # a -CITES-> shared exists twice: to the Document and to the Collection.
    with pytest.raises(lookup.AmbiguousMatchError) as exc:
        await writes.patch_edge(
            db_pool, tenant=TENANT, edge_type="CITES", src_id="a", dst_id="shared",
            patch={"w": 9},
        )
    assert exc.value.candidates == ["Document->Collection", "Document->Document"]

    edge = await writes.patch_edge(
        db_pool, tenant=TENANT, edge_type="CITES", src_id="a", dst_id="shared",
        dst_label="Document", patch={"w": 9, "note": None},
    )
    assert edge is not None
    assert (edge.dst_label, edge.properties) == ("Document", {"w": 9})
    async with db_pool.acquire() as conn:
        other = await lookup.find_edge(
            conn, tenant=TENANT, edge_type="CITES", src_id="a", dst_id="shared",
            dst_label="Collection",
        )
    assert other is not None
    assert other.properties == {"w": 2}

    assert await writes.delete_edge(
        db_pool, tenant=TENANT, edge_type="CITES", src_id="a", dst_id="shared",
        dst_label="Collection",
    ) == 1
    assert await writes.delete_edge(
        db_pool, tenant=TENANT, edge_type="CITES", src_id="a", dst_id="shared",
        dst_label="Collection",
    ) == 0
    # Now a single edge remains, so the ends alone name it; nodes stay.
    async with db_pool.acquire() as conn:
        left = await lookup.find_edge(
            conn, tenant=TENANT, edge_type="CITES", src_id="a", dst_id="shared",
        )
    assert left is not None
    assert left.properties == {"w": 9}
    assert await _node(db_pool, "shared", "Collection") == {"id": "shared", "kind": "coll"}


# ── HTTP ───────────────────────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_node_routes(api: httpx.AsyncClient) -> None:
    assert (await api.get("/nodes/shared")).status_code == HTTPStatus.CONFLICT
    resp = await api.get("/nodes/shared", params={"label": "Collection"})
    assert resp.json()["properties"] == {"id": "shared", "kind": "coll"}
    assert (await api.get("/nodes/shared", params={"label": "Nope"})).status_code == (
        HTTPStatus.BAD_REQUEST
    )

    resp = await api.patch(
        "/nodes/a", content=b'{"n": null, "meta": {"x": 5}}',
        headers={"content-type": "application/merge-patch+json"},
    )
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json()["properties"] == {
        "id": "a", "meta": {"x": 5, "y": 2}, "tags": ["p"],
    }
    resp = await api.patch("/nodes/a", json={"id": "b"})
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert (await api.patch("/nodes/shared", json={"x": 1})).status_code == HTTPStatus.CONFLICT
    assert (await api.patch("/nodes/nope", json={"x": 1})).status_code == HTTPStatus.NOT_FOUND

    resp = await api.post("/nodes/shared/properties", json={"properties": {"x": 1}})
    assert resp.status_code == HTTPStatus.CONFLICT

    resp = await api.delete("/nodes/shared", params={"label": "Collection"})
    assert resp.json()["nodes_deleted"] == 1
    assert (await api.get("/nodes/shared")).json()["label"] == "Document"


@requires_db
@pytest.mark.asyncio
async def test_edge_routes(api: httpx.AsyncClient) -> None:
    ends = {"src": "a", "dst": "shared"}
    assert (await api.get("/edges/CITES", params=ends)).status_code == HTTPStatus.CONFLICT
    assert (await api.get("/edges/NOPE", params=ends)).status_code == HTTPStatus.NOT_FOUND
    missing = {"src": "a", "dst": "zzz"}
    assert (await api.get("/edges/CITES", params=missing)).status_code == HTTPStatus.NOT_FOUND
    bad_label = {**ends, "dst_label": "Nope"}
    assert (await api.get("/edges/CITES", params=bad_label)).status_code == (
        HTTPStatus.BAD_REQUEST
    )

    to_coll = {**ends, "dst_label": "Collection"}
    resp = await api.get("/edges/CITES", params=to_coll)
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["etag"] == resp.headers["etag"]
    assert body | {"elapsed_ms": 0, "etag": ""} == {
        "tenant": TENANT, "type": "CITES", "src_id": "a", "src_label": "Document",
        "dst_id": "shared", "dst_label": "Collection", "properties": {"w": 2},
        "rev": 1, "etag": "", "elapsed_ms": 0,
    }

    resp = await api.patch(
        "/edges/CITES", params=to_coll, content=b'{"w": null, "seen": true}',
        headers={"content-type": "application/merge-patch+json"},
    )
    assert resp.json()["properties"] == {"seen": True}
    assert (await api.get("/edges/CITES", params=to_coll)).json()["properties"] == {"seen": True}

    assert (await api.delete("/edges/CITES", params=ends)).status_code == HTTPStatus.CONFLICT
    assert (await api.delete("/edges/CITES", params=to_coll)).json()["edges_deleted"] == 1
    assert (await api.delete("/edges/CITES", params=to_coll)).json()["edges_deleted"] == 0
    assert (await api.get("/edges/CITES", params=ends)).json()["dst_label"] == "Document"

"""DB tests: revisions, ETag / If-Match, and the shortest-path endpoint.

DB-gated.  The API is driven in-process through httpx's ASGI transport on
the test's own event loop, with the test pool.
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
from graphos.bootstrap import TenantSpec, bootstrap_tenant, register_tenant
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_revisions"
SPEC = TenantSpec(
    name=TENANT, vertex_labels=("Person", "Doc"), edge_labels=("KNOWS", "WROTE"),
)


async def _seed(pool: asyncpg.Pool) -> None:
    await register_tenant(pool, SPEC)
    async with pool.acquire() as conn:
        for t in ("KNOWS", "WROTE", "Person", "Doc"):
            await conn.execute(f'DELETE FROM "{TENANT}"."{t}";')
    await writes.upsert_nodes(pool, tenant=TENANT, label="Person", rows=[
        {"id": "a", "props": {"name": "Ada"}}, {"id": "b", "props": {}},
        {"id": "c", "props": {}}, {"id": "twin", "props": {}},
    ])
    await writes.upsert_nodes(pool, tenant=TENANT, label="Doc", rows=[
        {"id": "d", "props": {"title": "T"}}, {"id": "twin", "props": {}},
    ])
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Person", dst_label="Person", edge_type="KNOWS",
        rows=[{"src_id": "a", "dst_id": "b", "props": {"since": 1}},
              {"src_id": "b", "dst_id": "c", "props": {}}],
    )
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Person", dst_label="Doc", edge_type="WROTE",
        rows=[{"src_id": "a", "dst_id": "d", "props": {}},
              {"src_id": "c", "dst_id": "d", "props": {}}],
    )


async def _node(pool: asyncpg.Pool, external_id: str, label: str = "Person") -> lookup.NodeRow:
    async with pool.acquire() as conn:
        node = await lookup.find_node(conn, tenant=TENANT, external_id=external_id, label=label)
    assert node is not None
    return node


async def _edge_rev(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        edge = await lookup.find_edge(
            conn, tenant=TENANT, edge_type="KNOWS", src_id="a", dst_id="b",
        )
    assert edge is not None
    return edge.rev


def _client(pool: asyncpg.Pool, **settings: object) -> httpx.AsyncClient:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True, **settings))
    app.state.pool = pool
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://test/v1/{TENANT}/graph",
    )


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _seed(db_pool)
    async with _client(db_pool) as client:
        yield client


# ── revisions ──────────────────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_every_write_bumps_the_revision(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    assert (await _node(db_pool, "a")).rev == 1
    await writes.upsert_nodes(db_pool, tenant=TENANT, label="Person", rows=[
        {"id": "a", "props": {"name": "Ada"}},     # same values: still a write
    ])
    assert (await _node(db_pool, "a")).rev == 2
    await writes.merge_node_properties(db_pool, tenant=TENANT, external_id="a", properties={"x": 1})
    assert (await _node(db_pool, "a")).rev == 3
    patched = await writes.patch_node(db_pool, tenant=TENANT, external_id="a", patch={"x": None})
    assert patched is not None and patched.rev == 4
    assert (await _node(db_pool, "a")).rev == 4
    await writes.set_node_communities(
        db_pool, tenant=TENANT, assignments=[(int((await _node(db_pool, "a")).gid), 7)],
    )
    node = await _node(db_pool, "a")
    assert (node.rev, node.properties["community_id"]) == (5, 7)
    assert (await _node(db_pool, "b")).rev == 1    # untouched

    assert await _edge_rev(db_pool) == 1
    await writes.upsert_edges(
        db_pool, tenant=TENANT, src_label="Person", dst_label="Person", edge_type="KNOWS",
        rows=[{"src_id": "a", "dst_id": "b", "props": {"since": 2}}],
    )
    assert await _edge_rev(db_pool) == 2
    edge = await writes.patch_edge(
        db_pool, tenant=TENANT, edge_type="KNOWS", src_id="a", dst_id="b", patch={"w": 1},
    )
    assert edge is not None and edge.rev == 3 == await _edge_rev(db_pool)


@requires_db
@pytest.mark.asyncio
async def test_bootstrap_adds_the_column_to_existing_and_new_tables(db_pool: asyncpg.Pool) -> None:
    # A graph made before revisions existed: AGE's own tables, a row in each,
    # no graphos_rev anywhere.
    old = "graphos_test_pre_revisions"
    async with db_pool.acquire() as conn:
        await conn.execute('SET search_path = ag_catalog, "$user", public;')
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", old):
            await conn.execute(f"SELECT drop_graph('{old}', true);")
        await conn.execute(f"SELECT create_graph('{old}');")
        await conn.execute(f"SELECT create_vlabel('{old}', 'Person');")
        await conn.execute(f"SELECT create_elabel('{old}', 'KNOWS');")
        await conn.execute(
            f'INSERT INTO "{old}"."Person" (properties) VALUES '
            """('{"id": "a"}'::agtype), ('{"id": "b"}'::agtype);"""
        )
        assert not await conn.fetchval(_HAS_REV, old, "Person", lookup.REV)

    await bootstrap_tenant(db_pool, TenantSpec(
        name=old, vertex_labels=("Person", "Late"), edge_labels=("KNOWS",),
    ))
    async with db_pool.acquire() as conn:
        tables = [
            r["relname"] for r in await conn.fetch(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = $1 AND c.relkind = 'r';",
                old,
            )
        ]
        assert {"_ag_label_vertex", "_ag_label_edge", "Person", "Late", "KNOWS"} <= set(tables)
        for table in tables:
            if table.startswith("_ag_label_") or table in {"Person", "Late", "KNOWS"}:
                assert await conn.fetchval(_HAS_REV, old, table, lookup.REV), table
        node = await lookup.find_node(conn, tenant=old, external_id="a")
    assert node is not None and node.rev == 1     # existing rows read as revision 1


_HAS_REV = (
    "SELECT EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = $1 AND c.relname = $2 AND a.attname = $3 AND NOT a.attisdropped);"
)


# ── ETag / If-Match over HTTP ──────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_node_if_match(api: httpx.AsyncClient) -> None:
    read = await api.get("/nodes/a")
    etag = read.headers["etag"]
    assert read.json()["etag"] == etag and read.json()["rev"] == 1

    ok = await api.patch("/nodes/a", json={"x": 1}, headers={"If-Match": etag})
    assert ok.status_code == HTTPStatus.OK, ok.text
    new = ok.headers["etag"]
    assert new != etag and ok.json()["rev"] == 2 == int(new.strip('"').split(".")[1])

    stale = await api.patch("/nodes/a", json={"x": 2}, headers={"If-Match": etag})
    assert stale.status_code == HTTPStatus.PRECONDITION_FAILED
    assert stale.headers["etag"] == new
    assert (await api.get("/nodes/a")).json()["properties"]["x"] == 1   # nothing written

    either = f'"0.0", {new}'
    assert (await api.patch("/nodes/a", json={"x": 3}, headers={"If-Match": either})).is_success
    assert (await api.patch("/nodes/a", json={"x": 4}, headers={"If-Match": "*"})).is_success
    weak = f"W/{(await api.get('/nodes/a')).headers['etag']}"
    assert (await api.patch("/nodes/a", json={"x": 5}, headers={"If-Match": weak})).status_code == (
        HTTPStatus.PRECONDITION_FAILED
    )
    gone = await api.patch("/nodes/nope", json={"x": 1}, headers={"If-Match": "*"})
    assert gone.status_code == HTTPStatus.PRECONDITION_FAILED

    current = (await api.get("/nodes/a")).headers["etag"]
    merged = await api.post(
        "/nodes/a/properties", json={"properties": {"y": 1}}, headers={"If-Match": current},
    )
    assert merged.status_code == HTTPStatus.OK
    assert merged.json()["etag"] == merged.headers["etag"] != current
    stale_merge = await api.post(
        "/nodes/a/properties", json={"properties": {"y": 2}}, headers={"If-Match": current},
    )
    assert stale_merge.status_code == HTTPStatus.PRECONDITION_FAILED


@requires_db
@pytest.mark.asyncio
async def test_delete_with_if_match(api: httpx.AsyncClient) -> None:
    ends = {"src": "a", "dst": "b"}
    etag = (await api.get("/edges/KNOWS", params=ends)).headers["etag"]
    await api.patch("/edges/KNOWS", params=ends, json={"w": 1})          # someone else writes
    stale = await api.delete("/edges/KNOWS", params=ends, headers={"If-Match": etag})
    assert stale.status_code == HTTPStatus.PRECONDITION_FAILED
    current = stale.headers["etag"]
    done = await api.delete("/edges/KNOWS", params=ends, headers={"If-Match": current})
    assert done.json()["edges_deleted"] == 1

    # With If-Match the id must name one node.
    assert (await api.delete("/nodes/twin", headers={"If-Match": "*"})).status_code == (
        HTTPStatus.CONFLICT
    )
    doc = (await api.get("/nodes/twin", params={"label": "Doc"})).headers["etag"]
    resp = await api.delete("/nodes/twin", params={"label": "Doc"}, headers={"If-Match": doc})
    assert resp.json()["nodes_deleted"] == 1
    assert (await api.get("/nodes/twin")).json()["label"] == "Person"


@requires_db
@pytest.mark.asyncio
async def test_a_recreated_node_does_not_match_an_old_etag(
    db_pool: asyncpg.Pool, api: httpx.AsyncClient,
) -> None:
    old = (await api.get("/nodes/b")).headers["etag"]
    await api.delete("/nodes/b")
    await writes.upsert_nodes(
        db_pool, tenant=TENANT, label="Person", rows=[{"id": "b", "props": {}}],
    )
    again = await api.get("/nodes/b")
    assert again.json()["rev"] == 1 and again.headers["etag"] != old
    resp = await api.patch("/nodes/b", json={"x": 1}, headers={"If-Match": old})
    assert resp.status_code == HTTPStatus.PRECONDITION_FAILED


@requires_db
@pytest.mark.asyncio
async def test_if_match_can_be_required(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    async with _client(db_pool, require_if_match=True) as api:
        assert (await api.patch("/nodes/a", json={"x": 1})).status_code == (
            HTTPStatus.PRECONDITION_REQUIRED
        )
        assert (await api.delete("/edges/KNOWS", params={"src": "a", "dst": "b"})).status_code == (
            HTTPStatus.PRECONDITION_REQUIRED
        )
        etag = (await api.get("/nodes/a")).headers["etag"]
        assert (await api.patch("/nodes/a", json={"x": 1}, headers={"If-Match": etag})).is_success
        # Batch upserts are not single-row writes and need no precondition.
        nodes = {"nodes": [{"id": "e", "label": "Person", "properties": {}}]}
        assert (await api.post("/nodes", json=nodes)).is_success


# ── shortest paths ─────────────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_paths(api: httpx.AsyncClient) -> None:
    out = await api.post("/paths", json={"src": "a", "dst": "c", "direction": "out"})
    assert out.status_code == HTTPStatus.OK, out.text
    body = out.json()
    assert (body["hops"], body["truncated"]) == (2, False)
    (path,) = body["paths"]
    assert [(n["id"], n["label"]) for n in path["nodes"]] == [
        ("a", "Person"), ("b", "Person"), ("c", "Person"),
    ]
    assert [(e["type"], e["src_id"], e["dst_id"]) for e in path["edges"]] == [
        ("KNOWS", "a", "b"), ("KNOWS", "b", "c"),
    ]
    assert path["nodes"][0]["properties"] is None

    both = (await api.post("/paths", json={
        "src": "a", "dst": "c", "direction": "both", "limit": 10, "include_properties": True,
    })).json()
    routes = sorted([n["id"] for n in p["nodes"]] for p in both["paths"])
    assert (both["hops"], routes) == (2, [["a", "b", "c"], ["a", "d", "c"]])
    via_doc = next(p for p in both["paths"] if p["nodes"][1]["id"] == "d")
    assert via_doc["nodes"][1] == {
        "id": "d", "label": "Doc", "properties": {"id": "d", "title": "T"},
    }
    # The second WROTE edge runs c -> d, against the path.
    assert [(e["src_id"], e["dst_id"]) for e in via_doc["edges"]] == [("a", "d"), ("c", "d")]
    assert via_doc["edges"][0]["properties"] == {}

    none = (await api.post("/paths", json={
        "src": "a", "dst": "c", "direction": "out", "rel_types": ["WROTE"],
    })).json()
    assert (none["hops"], none["paths"]) == (None, [])
    assert (await api.post("/paths", json={"src": "a", "dst": "a"})).json()["hops"] == 0


@requires_db
@pytest.mark.asyncio
async def test_paths_errors(api: httpx.AsyncClient) -> None:
    assert (await api.post("/paths", json={"src": "a", "dst": "zz"})).status_code == (
        HTTPStatus.NOT_FOUND
    )
    assert (await api.post("/paths", json={"src": "twin", "dst": "a"})).status_code == (
        HTTPStatus.CONFLICT
    )
    ok = await api.post("/paths", json={"src": "twin", "src_label": "Person", "dst": "a"})
    assert ok.status_code == HTTPStatus.OK and ok.json()["hops"] is None
    bad = await api.post("/paths", json={"src": "a", "dst": "c", "rel_types": ["NOPE"]})
    assert bad.status_code == HTTPStatus.BAD_REQUEST
    too_far = await api.post("/paths", json={"src": "a", "dst": "c", "max_hops": 99})
    assert too_far.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

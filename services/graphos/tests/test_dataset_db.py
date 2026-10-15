"""DB tests: the dataset export (``graphos.dataset``) and importing it back.

A tenant exported, its schema imported under another name and its file
imported with ``mode=empty`` exports again line for line the same; the file
is framed by a header and an end line, can be gzip, filtered, and stripped of
computed values; the import refuses another format, a non-empty tenant in
``empty`` mode, and a body past the size cap, and says when a file was cut off.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos.app import create_app
from graphos.bootstrap import forget
from graphos.config import Settings

from .conftest import requires_db

SOURCE, COPY = "graphos_test_dataset_src", "graphos_test_dataset_copy"
SCHEMA: dict[str, Any] = {
    "name": SOURCE,
    "vertex_labels": ["Person", "Employee", "Doc"],
    "edge_labels": ["WROTE", "KNOWS"],
    "label_hierarchy": {"Employee": "Person"},
    "property_indexes": [{"label": "Doc", "properties": ["kind"]}],
}
NODES = [
    {"_kind": "node", "id": "p1", "label": "Person",
     "properties": {"name": "Ada", "community_id": "1", "graphos_pagerank": 0.5}},
    {"_kind": "node", "id": "e1", "label": "Employee",
     "properties": {"name": "Grace Hörmann", "community_id": "1", "tags": ["a", "b"]}},
    {"_kind": "node", "id": "d1", "label": "Doc",
     "properties": {"kind": "guide", "pages": 12, "draft": False, "meta": {"v": 1.5}}},
    {"_kind": "node", "id": "d2", "label": "Doc",
     "properties": {"kind": "note", "community_id": "2"}},
]
EDGES = [
    {"_kind": "edge", "src_id": "p1", "src_label": "Person", "dst_id": "d1", "dst_label": "Doc",
     "type": "WROTE", "properties": {"year": 2026}},
    {"_kind": "edge", "src_id": "e1", "src_label": "Employee", "dst_id": "d2", "dst_label": "Doc",
     "type": "WROTE", "properties": {}},
    {"_kind": "edge", "src_id": "p1", "src_label": "Person", "dst_id": "e1",
     "dst_label": "Employee", "type": "KNOWS", "properties": {"since": "2020"}},
]


def _ndjson(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for name in (SOURCE, COPY):
            if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", name):
                await conn.execute(f"SELECT drop_graph('{name}', true);")
            await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", name)
            await conn.execute(
                "DELETE FROM graphos_meta.unregistered_tenants WHERE name = $1;", name,
            )
            forget(name)


def _app(pool: asyncpg.Pool, **settings: Any) -> httpx.AsyncClient:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True, **settings))
    app.state.pool = pool
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _fresh(db_pool)
    async with _app(db_pool) as client:
        assert (await client.post("/v1/tenants", json=SCHEMA)).status_code == HTTPStatus.OK
        resp = await client.post(
            f"/v1/{SOURCE}/graph/snapshot-import", content=_ndjson(NODES + EDGES),
            headers={"content-type": "application/x-ndjson"},
        )
        assert resp.status_code == HTTPStatus.OK, resp.text
        assert resp.json()["edges_upserted"] == len(EDGES)
        yield client
    await _fresh(db_pool)


async def _export(api: httpx.AsyncClient, tenant: str, **params: Any) -> list[dict[str, Any]]:
    resp = await api.get(f"/v1/{tenant}/graph/snapshot", params=params)
    assert resp.status_code == HTTPStatus.OK, resp.text
    return [json.loads(line) for line in resp.text.splitlines()]


def _body(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r["_kind"] in ("node", "edge")]


@requires_db
@pytest.mark.asyncio
async def test_the_export_is_framed_and_lists_each_node_once_with_its_own_label(
    api: httpx.AsyncClient,
) -> None:
    resp = await api.get(f"/v1/{SOURCE}/graph/snapshot")
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert resp.headers["content-disposition"] == f'attachment; filename="{SOURCE}.ndjson"'
    rows = [json.loads(line) for line in resp.text.splitlines()]
    header, end = rows[0], rows[-1]
    assert (header["_kind"], header["format"], header["version"]) == (
        "header", "graphos.snapshot", 1,
    )
    assert header["tenant"] == SOURCE and header["computed"] is True
    assert header["counts"] == {"nodes": 4, "edges": 3}
    assert header["schema"]["label_hierarchy"] == {"Employee": "Person"}
    assert end == {"_kind": "end", "counts": {"nodes": 4, "edges": 3}}
    nodes = [r for r in rows if r["_kind"] == "node"]
    assert sorted((n["id"], n["label"]) for n in nodes) == [
        ("d1", "Doc"), ("d2", "Doc"), ("e1", "Employee"), ("p1", "Person"),
    ]
    by_id = {n["id"]: n for n in nodes}
    assert by_id["e1"]["properties"]["name"] == "Grace Hörmann"
    assert by_id["d1"]["properties"]["meta"] == {"v": 1.5}
    assert by_id["p1"]["properties"]["graphos_pagerank"] == 0.5
    # Nodes before edges; each edge names its ends' own labels.
    kinds = [r["_kind"] for r in rows[1:-1]]
    assert kinds == sorted(kinds, key=lambda k: k != "node")
    assert {(e["src_id"], e["src_label"], e["type"], e["dst_id"]) for e in _body(rows)
            if e["_kind"] == "edge"} == {
        ("p1", "Person", "WROTE", "d1"), ("e1", "Employee", "WROTE", "d2"),
        ("p1", "Person", "KNOWS", "e1"),
    }


@requires_db
@pytest.mark.asyncio
async def test_an_export_restores_into_an_empty_tenant_exactly(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    resp = await api.get(f"/v1/{SOURCE}/graph/snapshot")
    text = resp.text
    header = json.loads(text.splitlines()[0])
    # The schema first (what a client does with the header), then the data.
    registered = await api.post("/v1/tenants", json={**header["schema"], "name": COPY})
    assert registered.status_code == HTTPStatus.OK
    imported = await api.post(
        f"/v1/{COPY}/graph/snapshot-import", params={"mode": "empty"}, content=text,
        headers={"content-type": "application/x-ndjson"},
    )
    assert imported.status_code == HTTPStatus.OK, imported.text
    result = imported.json()
    assert (result["mode"], result["complete"], result["errors"]) == ("empty", True, [])
    assert result["header"]["tenant"] == SOURCE and "schema" not in result["header"]
    assert (result["nodes_upserted"], result["edges_upserted"]) == (4, 3)
    assert result["edge_communities_set"] > 0
    copied, original = _body(await _export(api, COPY)), _body(await _export(api, SOURCE))
    nodes = lambda rows: [r for r in rows if r["_kind"] == "node"]  # noqa: E731
    edges = lambda rows: sorted(json.dumps(r) for r in rows if r["_kind"] == "edge")  # noqa: E731
    assert nodes(copied) == nodes(original)                  # in the same order
    assert edges(copied) == edges(original)                  # in no set order within a type
    # The edges' copies of their ends' communities came back with the nodes'.
    async with db_pool.acquire() as conn:
        knows = await conn.fetchrow(
            f'SELECT start_community_id, end_community_id FROM "{COPY}"."KNOWS";',
        )
    assert (knows["start_community_id"], knows["end_community_id"]) == ("1", "1")

    again = await api.post(
        f"/v1/{COPY}/graph/snapshot-import", params={"mode": "empty"}, content=text,
        headers={"content-type": "application/x-ndjson"},
    )
    assert again.status_code == HTTPStatus.CONFLICT and "holds nodes" in again.text


@requires_db
@pytest.mark.asyncio
async def test_gzip_both_ways(api: httpx.AsyncClient) -> None:
    plain = (await api.get(f"/v1/{SOURCE}/graph/snapshot")).text
    resp = await api.get(f"/v1/{SOURCE}/graph/snapshot", params={"gzip": "true"})
    assert resp.headers["content-type"] == "application/gzip"
    assert resp.headers["content-disposition"].endswith('.ndjson.gz"')
    unpacked = gzip.decompress(resp.content).decode("utf-8")
    strip = lambda t: [line for line in t.splitlines() if '"_kind":"header"' not in line]  # noqa: E731
    assert strip(unpacked) == strip(plain)
    imported = await api.post(f"/v1/{SOURCE}/graph/snapshot-import", content=resp.content)
    assert imported.status_code == HTTPStatus.OK, imported.text
    assert imported.json()["complete"] is True and imported.json()["nodes_upserted"] == 4  # noqa: PLR2004


@requires_db
@pytest.mark.asyncio
async def test_filters_and_computed_values(api: httpx.AsyncClient) -> None:
    rows = await _export(api, SOURCE, labels=["Person", "Doc"], types=["WROTE"])
    assert {(r["id"], r["label"]) for r in rows if r["_kind"] == "node"} == {
        ("p1", "Person"), ("d1", "Doc"), ("d2", "Doc"),
    }
    # e1 is an Employee, not exported: its WROTE edge is left out too.
    assert [(r["src_id"], r["dst_id"]) for r in rows if r["_kind"] == "edge"] == [("p1", "d1")]
    assert rows[-1]["counts"] == {"nodes": 3, "edges": 1}
    bare = await _export(api, SOURCE, computed="false")
    assert bare[0]["computed"] is False
    for node in (r for r in bare if r["_kind"] == "node"):
        computed = {k for k in node["properties"]
                    if k.startswith("graphos_") or k == "community_id"}
        assert not computed
    unknown = await api.get(f"/v1/{SOURCE}/graph/snapshot", params={"labels": ["Nope"]})
    assert unknown.status_code == HTTPStatus.BAD_REQUEST


@requires_db
@pytest.mark.asyncio
async def test_what_the_import_refuses_or_reports(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    text = (await api.get(f"/v1/{SOURCE}/graph/snapshot")).text
    newer = text.replace('"version":1', '"version":2', 1)
    resp = await api.post(f"/v1/{SOURCE}/graph/snapshot-import", content=newer)
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY and "version 2" in resp.text
    cut = "\n".join(text.splitlines()[:-1]) + "\n"           # without its end line
    resp = await api.post(f"/v1/{SOURCE}/graph/snapshot-import", content=cut)
    assert resp.status_code == HTTPStatus.OK and resp.json()["complete"] is False
    plain = await api.post(f"/v1/{SOURCE}/graph/snapshot-import", content=_ndjson(NODES[:1]))
    assert plain.json()["header"] is None and plain.json()["complete"] is None
    # A small cap: a gzip body that expands past it stops with 413.
    async with _app(db_pool, import_max_bytes=2000) as capped:
        bomb = gzip.compress((_ndjson(NODES[:1]) * 200).encode())
        resp = await capped.post(f"/v1/{SOURCE}/graph/snapshot-import", content=bomb)
        assert resp.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE, resp.text


@requires_db
@pytest.mark.asyncio
async def test_an_export_is_audited_as_a_read(
    api: httpx.AsyncClient, caplog: pytest.LogCaptureFixture,
) -> None:
    # Both exports hand out a lot at once; the schema is an ordinary read.
    with caplog.at_level(logging.INFO, logger="graphos.audit"):
        await api.get(f"/v1/{SOURCE}/graph/snapshot")
        await api.get(f"/v1/{SOURCE}/graph/export", params={"limit_nodes": 10})
        await api.get(f"/v1/{SOURCE}/schema")
    audited = [(r.getMessage(), r.path) for r in caplog.records if r.name == "graphos.audit"]
    assert audited == [
        ("read", f"/v1/{SOURCE}/graph/snapshot"), ("read", f"/v1/{SOURCE}/graph/export"),
    ]

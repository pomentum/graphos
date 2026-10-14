"""DB tests: graph metrics written onto nodes (graphos.graph_metrics), and graphos_* reserved.

DB-gated. The API is driven in-process through httpx's ASGI transport with the
test pool. A star (four functions calling one), a chain of three, and one node
with no edge.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import analysis, graph_metrics, writes
from graphos.app import create_app
from graphos.bootstrap import forget
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_metrics"
STAR = [("s1", "hub"), ("s2", "hub"), ("s3", "hub"), ("s4", "hub")]
CHAIN = [("c1", "c2"), ("c2", "c3")]
NODES = ["hub", "s1", "s2", "s3", "s4", "c1", "c2", "c3", "alone"]


def _app(pool: asyncpg.Pool) -> httpx.AsyncClient:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = pool
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _edge(src: str, dst: str, type_: str = "CALLS") -> dict[str, str]:
    return {"src_id": src, "src_label": "Function", "dst_id": dst, "dst_label": "Function",
            "type": type_}


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", TENANT):
            await conn.execute(f"SELECT drop_graph('{TENANT}', true);")
        await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", TENANT)
        await conn.execute(
            "DELETE FROM graphos_meta.unregistered_tenants WHERE name = $1;", TENANT,
        )
    forget(TENANT)


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _fresh(db_pool)
    async with _app(db_pool) as client:
        resp = await client.post("/v1/tenants", json={
            "name": TENANT, "vertex_labels": ["Function"], "edge_labels": ["CALLS", "USES"],
        })
        assert resp.status_code == HTTPStatus.OK, resp.text
        await writes.upsert_nodes(db_pool, tenant=TENANT, label="Function", rows=[
            {"id": n, "props": {"name": n}} for n in NODES
        ])
        resp = await client.post(f"/v1/{TENANT}/graph/edges", json={
            "edges": [_edge(s, d) for s, d in STAR + CHAIN],
        })
        assert resp.status_code == HTTPStatus.OK, resp.text
        yield client
    graph_metrics.configure(betweenness_max_nodes=20_000)
    await _fresh(db_pool)


async def _compute(api: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    resp = await api.post(f"/v1/{TENANT}/graph/metrics:compute", json=body)
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()


async def _node(api: httpx.AsyncClient, node_id: str) -> dict[str, Any]:
    resp = await api.get(f"/v1/{TENANT}/graph/nodes/{node_id}")
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()


# ── computing and writing back ──────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_metrics_are_written_onto_the_nodes(api: httpx.AsyncClient) -> None:
    head = (await api.get(f"/v1/{TENANT}/changes/head")).json()["cursor"]
    body = await _compute(api)
    assert body["metrics"] == ["degree", "pagerank", "components"]
    assert body["n_nodes"] == 8 and body["n_edges_scanned"] == 6     # "alone" has no edge
    assert body["n_changed"] == 8 and body["n_components"] == 2
    hub = (await _node(api, "hub"))["properties"]
    assert (hub["graphos_degree"], hub["graphos_in_degree"], hub["graphos_out_degree"]) == (4, 4, 0)
    assert hub["graphos_component"] == 0                          # the star is the larger one
    assert (await _node(api, "c2"))["properties"]["graphos_component"] == 1
    assert hub["graphos_pagerank"] > (await _node(api, "s1"))["properties"]["graphos_pagerank"]
    assert not any(k.startswith("graphos_") for k in (await _node(api, "alone"))["properties"])
    # One change record summarises the run, and the feed reads it.
    feed = await api.get(f"/v1/{TENANT}/changes", params={"after": head, "limit": 50})
    assert feed.status_code == HTTPStatus.OK, feed.text
    runs = [c for c in feed.json()["changes"] if c["op"] == "metrics"]
    assert len(runs) == 1 and runs[0]["count"] == 8 and runs[0]["kind"] == "node"


@requires_db
@pytest.mark.asyncio
async def test_a_rerun_on_an_unchanged_graph_writes_nothing(api: httpx.AsyncClient) -> None:
    await _compute(api, metrics=["degree", "pagerank", "components", "betweenness"])
    before = (await _node(api, "hub"))["rev"]
    again = await _compute(api, metrics=["degree", "pagerank", "components", "betweenness"])
    assert again["n_changed"] == 0 and again["n_cleared"] == 0
    assert (await _node(api, "hub"))["rev"] == before            # no revision bump


@requires_db
@pytest.mark.asyncio
async def test_only_changed_nodes_are_written_and_ids_stay(api: httpx.AsyncClient) -> None:
    await _compute(api, metrics=["degree", "components"])
    untouched_rev = (await _node(api, "s1"))["rev"]
    # c3 -> alone: the chain grows by one node; the star does not change.
    resp = await api.post(f"/v1/{TENANT}/graph/edges", json={"edges": [_edge("c3", "alone")]})
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = await _compute(api, metrics=["degree", "components"])
    assert body["n_changed"] == 2                                 # c3 (degree) and alone (new)
    assert (await _node(api, "s1"))["rev"] == untouched_rev
    assert (await _node(api, "alone"))["properties"]["graphos_component"] == 1
    assert (await _node(api, "hub"))["properties"]["graphos_component"] == 0


@requires_db
@pytest.mark.asyncio
async def test_writes_in_batches_still_one_run(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Several statements per run (3 nodes each), still one transaction and one record.
    monkeypatch.setattr(writes, "METRICS_BATCH", 3)
    head = (await api.get(f"/v1/{TENANT}/changes/head")).json()["cursor"]
    body = await _compute(api, metrics=["degree", "components"])
    assert body["n_changed"] == 8
    for node_id in ["hub", "s1", "s2", "s3", "s4", "c1", "c2", "c3"]:
        assert "graphos_degree" in (await _node(api, node_id))["properties"], node_id
    feed = await api.get(f"/v1/{TENANT}/changes", params={"after": head, "limit": 50})
    runs = [c for c in feed.json()["changes"] if c["op"] == "metrics"]
    assert len(runs) == 1 and runs[0]["count"] == 8
    # Clears batch too: the star loses all its edges, five nodes lose their values.
    for src, dst in STAR:
        resp = await api.delete(
            f"/v1/{TENANT}/graph/edges/CALLS",
            params={"src": src, "dst": dst, "src_label": "Function", "dst_label": "Function"},
        )
        assert resp.status_code in (HTTPStatus.OK, HTTPStatus.NO_CONTENT), resp.text
    body = await _compute(api, metrics=["degree", "components"])
    assert body["n_cleared"] == 5
    for node_id in ["hub", "s1", "s2", "s3", "s4"]:
        assert not any(k.startswith("graphos_") for k in (await _node(api, node_id))["properties"])


@requires_db
@pytest.mark.asyncio
async def test_a_node_that_loses_its_edges_loses_its_values(api: httpx.AsyncClient) -> None:
    await _compute(api, metrics=["degree"])
    resp = await api.delete(
        f"/v1/{TENANT}/graph/edges/CALLS",
        params={"src": "s4", "dst": "hub", "src_label": "Function", "dst_label": "Function"},
    )
    assert resp.status_code in (HTTPStatus.OK, HTTPStatus.NO_CONTENT), resp.text
    body = await _compute(api, metrics=["degree"])
    assert body["n_cleared"] == 1
    assert not any(k.startswith("graphos_") for k in (await _node(api, "s4"))["properties"])
    assert (await _node(api, "hub"))["properties"]["graphos_degree"] == 3


@requires_db
@pytest.mark.asyncio
async def test_dry_run_writes_nothing_and_types_filter(api: httpx.AsyncClient) -> None:
    dry = await _compute(api, dry_run=True)
    assert dry["n_changed"] == 8
    assert not any(k.startswith("graphos_") for k in (await _node(api, "hub"))["properties"])
    none = await _compute(api, rel_types=["USES"], metrics=["degree"])
    assert none["n_nodes"] == 0 and none["n_edges_scanned"] == 0


@requires_db
@pytest.mark.asyncio
async def test_betweenness_is_skipped_on_a_graph_too_large(api: httpx.AsyncClient) -> None:
    graph_metrics.configure(betweenness_max_nodes=3)
    body = await _compute(api, metrics=["betweenness"])
    assert body["betweenness_skipped"] and "GRAPHOS_BETWEENNESS_MAX_NODES" in body["warnings"][0]
    assert body["properties"] == []


@requires_db
@pytest.mark.asyncio
async def test_the_top_nodes_by_a_metric_come_from_nodes_scan(api: httpx.AsyncClient) -> None:
    await _compute(api, metrics=["pagerank"])
    resp = await api.post(f"/v1/{TENANT}/graph/nodes:scan", json={
        "labels": ["Function"], "order_by": [{"property": "graphos_pagerank", "desc": True}],
        "project": ["graphos_pagerank"], "limit": 3,
    })
    assert resp.status_code == HTTPStatus.OK, resp.text
    items = resp.json()["items"]
    scores = [i["properties"]["graphos_pagerank"] for i in items]
    assert scores == sorted(scores, reverse=True) and len(items) == 3
    assert "alone" not in {i["id"] for i in items}                # no value: not ranked


@requires_db
@pytest.mark.asyncio
async def test_one_whole_graph_job_per_tenant(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    async with analysis.tenant_lock(db_pool, TENANT):
        resp = await api.post(f"/v1/{TENANT}/graph/metrics:compute", json={})
        assert resp.status_code == HTTPStatus.CONFLICT
        assert "whole-graph job" in resp.json()["detail"]
    assert (await api.post(f"/v1/{TENANT}/graph/metrics:compute", json={})).status_code == 200


# ── graphos_* is reserved ───────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_writes_cannot_use_graphos_properties(api: httpx.AsyncClient) -> None:
    graph = f"/v1/{TENANT}/graph"
    refused = [
        await api.post(f"{graph}/nodes", json={"nodes": [
            {"id": "x", "label": "Function", "properties": {"graphos_pagerank": 1}},
        ]}),
        await api.patch(f"{graph}/nodes/hub", json={"graphos_degree": 9},
                        headers={"content-type": "application/merge-patch+json"}),
        await api.patch(f"{graph}/nodes/hub", json={"graphos_degree": None},
                        headers={"content-type": "application/merge-patch+json"}),
        await api.post(f"{graph}/nodes/hub/properties", json={"properties": {"graphos_x": 1}}),
        await api.post(f"{graph}/edges", json={"edges": [
            {**_edge("s1", "s2"), "properties": {"graphos_weight": 1}},
        ]}),
        await api.patch(f"{graph}/edges/CALLS", params={"src": "s1", "dst": "hub"},
                        json={"graphos_w": 1},
                        headers={"content-type": "application/merge-patch+json"}),
    ]
    for resp in refused:
        assert resp.status_code == HTTPStatus.BAD_REQUEST, resp.text
        assert "reserved" in resp.json()["detail"]
    # Bulk: the line is invalid, the others go through.
    lines = [
        {"id": "b1", "label": "Function", "properties": {"name": "b1"}},
        {"id": "b2", "label": "Function", "properties": {"graphos_component": 3}},
    ]
    resp = await api.post(
        f"{graph}/nodes:bulk", content="\n".join(json.dumps(x) for x in lines) + "\n",
        headers={"content-type": "application/x-ndjson"},
    )
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json()["rows_invalid"] == 1 and "reserved" in resp.json()["errors"][0]
    # Ordinary names still work.
    ok = await api.post(f"{graph}/nodes/hub/properties", json={"properties": {"graphos": 1}})
    assert ok.status_code == HTTPStatus.OK, ok.text

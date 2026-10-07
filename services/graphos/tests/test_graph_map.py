"""DB tests: the graph map (labels and the links between them) and hubs.

Counts are checked exactly on small tables, and again with sampling forced
on (every table counts as large) to run the sampled SQL; at these sizes a
SYSTEM sample of the planner's estimate reads every block, so the scaled
counts still match.
"""

from __future__ import annotations

from http import HTTPStatus

import asyncpg
import httpx
import pytest

from graphos import writes
from graphos.api import graph_map
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_graph_map"


async def _seed(pool: asyncpg.Pool) -> None:
    await register_tenant(pool, TenantSpec(
        name=TENANT, vertex_labels=("Class", "Function", "Empty"), edge_labels=("CALLS", "DEFINES"),
    ))
    async with pool.acquire() as conn:
        for t in ("CALLS", "DEFINES", "Class", "Function", "Empty"):
            await conn.execute(f'DELETE FROM "{TENANT}"."{t}";')
    await writes.upsert_nodes(pool, tenant=TENANT, label="Class", rows=[
        {"id": "c1", "props": {"name": "Invoice"}}, {"id": "c2", "props": {"name": "Cart"}},
    ])
    await writes.upsert_nodes(pool, tenant=TENANT, label="Function", rows=[
        {"id": f"f{i}", "props": {"name": f"f{i}"}} for i in range(5)
    ])
    # c1 defines f0..f3, c2 defines f4; f0 calls every other function.
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Class", dst_label="Function", edge_type="DEFINES",
        rows=[{"src_id": "c1", "dst_id": f"f{i}", "props": {}} for i in range(4)]
        + [{"src_id": "c2", "dst_id": "f4", "props": {}}],
    )
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Function", dst_label="Function", edge_type="CALLS",
        rows=[{"src_id": "f0", "dst_id": f"f{i}", "props": {}} for i in range(1, 5)],
    )
    async with pool.acquire() as conn:
        for t in ("CALLS", "DEFINES", "Class", "Function"):
            await conn.execute(f'ANALYZE "{TENANT}"."{t}";')


def _client(pool: asyncpg.Pool) -> httpx.AsyncClient:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = pool
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://test/v1/{TENANT}/graph",
    )


async def _map(api: httpx.AsyncClient, **params: str) -> dict:
    resp = await api.get("/map", params=params)
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize("sampled", [False, True])
async def test_map_counts_labels_and_links(
    db_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, sampled: bool,
) -> None:
    await _seed(db_pool)
    if sampled:
        monkeypatch.setattr(graph_map, "SAMPLE_ABOVE", -1)
        monkeypatch.setattr(graph_map, "SAMPLE_ROWS", 10**9)  # 100%: every block
    async with _client(db_pool) as api:
        body = await _map(api)
    assert [(r["label"], r["count"]) for r in body["labels"]] == [("Function", 5), ("Class", 2)]
    links = {(r["src_label"], r["type"], r["dst_label"]): r["count"] for r in body["links"]}
    assert links == {("Class", "DEFINES", "Function"): 5, ("Function", "CALLS", "Function"): 4}
    assert body["estimated"] is False  # a 100% sample is scaled by 1


@requires_db
@pytest.mark.asyncio
async def test_hubs_are_the_best_connected_nodes_of_a_label(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    async with _client(db_pool) as api:
        resp = await api.get("/map/hubs", params={"label": "Function", "limit": 2})
        assert resp.status_code == HTTPStatus.OK, resp.text
        hubs = resp.json()["nodes"]
        # f0: defined by c1 + calls four functions = 5 edges.
        assert [(h["id"], h["degree"]) for h in hubs][0] == ("f0", 5)
        assert len(hubs) == 2 and hubs[1]["degree"] == 2
        assert hubs[0]["properties"]["name"] == "f0"

        classes = (await api.get("/map/hubs", params={"label": "Class"})).json()["nodes"]
        assert [(h["id"], h["degree"]) for h in classes] == [("c1", 4), ("c2", 1)]

        missing = await api.get("/map/hubs", params={"label": "Nope"})
        assert missing.status_code == HTTPStatus.NOT_FOUND

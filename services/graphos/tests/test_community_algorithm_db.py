"""The community algorithm (ADR-0011): Louvain by default, Leiden by request or
per tenant, Leiden starting from the stored partition when it can.

DB-gated, and needs the optional ``louvain`` extra (python-igraph).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import louvain_compute as lc
from graphos import writes
from graphos.app import create_app
from graphos.bootstrap import forget
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_community_algorithm"
pytestmark = pytest.mark.skipif(lc.ig is None, reason="louvain extra not installed")


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", TENANT):
            await conn.execute(f"SELECT drop_graph('{TENANT}', true);")
        for table, column in (("tenants", "name"), ("unregistered_tenants", "name"),
                              ("analysis_runs", "tenant"), ("community_map_runs", "tenant")):
            await conn.execute(f"DELETE FROM graphos_meta.{table} WHERE {column} = $1;", TENANT)
    forget(TENANT)


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _fresh(db_pool)
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        resp = await client.post("/v1/tenants", json={
            "name": TENANT, "vertex_labels": ["Function"], "edge_labels": ["CALLS", "USES"],
        })
        assert resp.status_code == HTTPStatus.OK, resp.text
        # Two cliques of six joined by one edge, and one USES edge.
        names = [f"{side}{i}" for side in "pq" for i in range(6)]
        await writes.upsert_nodes(db_pool, tenant=TENANT, label="Function", rows=[
            {"id": n, "props": {"name": n}} for n in names
        ])
        rows = [
            {"src_id": f"{side}{i}", "dst_id": f"{side}{j}", "props": {}}
            for side in "pq" for i in range(6) for j in range(i + 1, 6)
        ] + [{"src_id": "p0", "dst_id": "q0", "props": {}}]
        await writes.upsert_edges(db_pool, tenant=TENANT, src_label="Function",
                                  dst_label="Function", edge_type="CALLS", rows=rows)
        await writes.upsert_edges(db_pool, tenant=TENANT, src_label="Function",
                                  dst_label="Function", edge_type="USES",
                                  rows=[{"src_id": "p1", "dst_id": "q1", "props": {}}])
        yield client
    await _fresh(db_pool)


async def _setting(api: httpx.AsyncClient) -> str:
    resp = await api.get(f"/v1/tenants/{TENANT}")
    assert resp.status_code == HTTPStatus.OK, resp.text
    return str(resp.json()["community_algorithm"])


async def _register(api: httpx.AsyncClient, **extra: Any) -> None:
    resp = await api.post("/v1/tenants", json={"name": TENANT, **extra})
    assert resp.status_code == HTTPStatus.OK, resp.text


async def _recompute(api: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    resp = await api.post(f"/v1/{TENANT}/recompute-communities",
                          json={"min_community_size": 2, **body})
    assert resp.status_code == HTTPStatus.OK, resp.text
    return dict(resp.json())


@requires_db
@pytest.mark.asyncio
async def test_a_tenant_has_louvain_until_it_asks_for_leiden(api: httpx.AsyncClient) -> None:
    assert await _setting(api) == "louvain"                    # never empty
    await _register(api, community_algorithm="leiden")
    assert await _setting(api) == "leiden"
    await _register(api, vertex_labels=["Function"])           # omitted: kept
    assert await _setting(api) == "leiden"
    await _register(api, community_algorithm="louvain")
    assert await _setting(api) == "louvain"


@requires_db
@pytest.mark.asyncio
async def test_a_request_without_an_algorithm_runs_the_tenants(api: httpx.AsyncClient) -> None:
    body = await _recompute(api)
    assert (body["algorithm"], body["from_scratch"]) == ("louvain", True)
    await _register(api, community_algorithm="leiden")
    first = await _recompute(api)                     # the stored partition is Louvain's
    assert (first["algorithm"], first["from_scratch"]) == ("leiden", True)
    second = await _recompute(api)
    assert (second["algorithm"], second["from_scratch"]) == ("leiden", False)
    assert second["n_communities"] == 2
    louvain = await _recompute(api, algorithm="louvain")      # the request wins
    assert (louvain["algorithm"], louvain["from_scratch"]) == ("louvain", True)


@requires_db
@pytest.mark.asyncio
async def test_leiden_starts_from_scratch_when_the_stored_run_answers_another_question(
    api: httpx.AsyncClient,
) -> None:
    await _recompute(api, algorithm="leiden")
    assert (await _recompute(api, algorithm="leiden"))["from_scratch"] is False
    assert (await _recompute(api, algorithm="leiden", from_scratch=True))["from_scratch"] is True
    scoped = await _recompute(api, algorithm="leiden", rel_types=["CALLS"])
    assert scoped["from_scratch"] is True                     # another scope
    assert (await _recompute(api, algorithm="leiden", rel_types=["CALLS"]))["from_scratch"] is False
    assert (await _recompute(api, algorithm="leiden", rel_types=["CALLS"],
                             resolution=0.5))["from_scratch"] is True   # another resolution


@requires_db
@pytest.mark.asyncio
async def test_info_lists_the_algorithms(api: httpx.AsyncClient) -> None:
    features = (await api.get("/v1/info")).json()["features"]
    assert features["community_algorithms"] == ["louvain", "leiden"]

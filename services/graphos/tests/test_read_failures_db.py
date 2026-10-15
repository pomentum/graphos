"""DB tests: a read the database refuses answers an error, never a smaller result.

The edge tables are broken for real (a column the hop queries read is
renamed), so neighbors and expand meet a genuine database error; before,
they skipped the hop and answered as if there were fewer neighbours.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos.app import create_app
from graphos.bootstrap import forget
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_read_failures"
G = f"/v1/{TENANT}/graph"


async def _drop(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", TENANT):
            await conn.execute(f"SELECT drop_graph('{TENANT}', true);")
        for table in ("tenants", "unregistered_tenants"):
            await conn.execute(f"DELETE FROM graphos_meta.{table} WHERE name = $1;", TENANT)
    forget(TENANT)


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _drop(db_pool)
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        body = {"name": TENANT, "vertex_labels": ["Doc"], "edge_labels": ["CITES"]}
        assert (await client.post("/v1/tenants", json=body)).status_code == HTTPStatus.OK
        nodes = [{"id": i, "label": "Doc", "properties": {"name": i}} for i in ("a", "b")]
        written = await client.post(f"{G}/nodes", json={"nodes": nodes})
        assert written.status_code == HTTPStatus.OK
        edge = {"src_id": "a", "src_label": "Doc", "dst_id": "b", "dst_label": "Doc",
                "type": "CITES"}
        written = await client.post(f"{G}/edges", json={"edges": [edge]})
        assert written.status_code == HTTPStatus.OK
        yield client
    await _drop(db_pool)


@requires_db
@pytest.mark.asyncio
async def test_a_failed_hop_is_an_error_not_fewer_neighbours(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    neighbors = await api.get(f"{G}/neighbors", params={"node_id": "a"})
    assert neighbors.status_code == HTTPStatus.OK
    assert len(neighbors.json()["neighbors"]) == 1

    # Every edge table loses the column the hop queries read.
    await db_pool.execute(
        f'ALTER TABLE "{TENANT}"."_ag_label_edge" RENAME COLUMN end_id TO broken;',
    )

    neighbors = await api.get(f"{G}/neighbors", params={"node_id": "a"})
    assert neighbors.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert neighbors.json()["detail"].startswith("neighbors failed in the database (42703)")
    expand = await api.get(f"{G}/expand", params={"node_id": "a", "max_hops": 2})
    assert expand.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert expand.json()["detail"].startswith("expand (hop 1) failed in the database")


@requires_db
@pytest.mark.asyncio
async def test_a_sampled_type_without_a_table_has_no_edges(api: httpx.AsyncClient) -> None:
    body = {"label": "Doc", "rel_types": ["CITES", "NEVER_CREATED"], "include_properties": ["name"]}
    resp = await api.post(f"{G}/sample_subgraph", json=body)
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert [e["type"] for e in resp.json()["edges"]] == ["CITES"]

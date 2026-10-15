"""Capped neighbours and expand answer the same rows however the table is stored.

DB-gated. A hub with more neighbours than the cap: the rows returned are the
first neighbours in graph-id order (the order nodes were loaded in), not the
first the table happens to hold, so moving rows around, or a dump and a
restore, gives the same answer.
"""

from __future__ import annotations

import random
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

TENANT = "graphos_test_capped_order"
G = f"/v1/{TENANT}/graph"
LEAVES = [f"l{i:02d}" for i in range(30)]


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
        nodes = [{"id": i, "label": "Doc", "properties": {"name": i}} for i in ["hub", *LEAVES]]
        assert (await client.post(f"{G}/nodes", json={"nodes": nodes})).status_code == HTTPStatus.OK
        shuffled = LEAVES[:]
        random.Random(7).shuffle(shuffled)
        edges = [{"src_id": "hub", "src_label": "Doc", "dst_id": leaf, "dst_label": "Doc",
                  "type": "CITES"} for leaf in shuffled]
        assert (await client.post(f"{G}/edges", json={"edges": edges})).status_code == HTTPStatus.OK
        # Rewrite the edges to the first leaves: their rows move to the end of the table.
        await db_pool.execute(
            f'UPDATE "{TENANT}"."CITES" e SET properties = e.properties '
            f'FROM "{TENANT}"."Doc" v WHERE v.id = e.end_id '
            f"AND (v.properties -> '\"id\"'::agtype) IN "
            + "(" + ", ".join(f"'\"{leaf}\"'::agtype" for leaf in LEAVES[:10]) + ");",
        )
        yield client
    await _drop(db_pool)


@requires_db
@pytest.mark.asyncio
async def test_capped_neighbours_are_the_first_in_graph_id_order(api: httpx.AsyncClient) -> None:
    resp = await api.get(f"{G}/neighbors", params={"node_id": "hub", "limit": 10})
    assert resp.status_code == HTTPStatus.OK
    body = resp.json()
    assert body["truncated"] is True
    assert sorted(n["neighbor_id"] for n in body["neighbors"]) == LEAVES[:10]


@requires_db
@pytest.mark.asyncio
async def test_capped_expand_is_the_same_whatever_the_storage_order(
    api: httpx.AsyncClient,
) -> None:
    params = {"node_id": "hub", "max_hops": 1, "direction": "out", "limit": 10}
    first = (await api.get(f"{G}/expand", params=params)).json()
    assert first["truncated"] is True
    ends = sorted(p["end_id"] for p in first["paths"])
    assert ends == LEAVES[:10]
    again = (await api.get(f"{G}/expand", params=params)).json()
    assert sorted(p["end_id"] for p in again["paths"]) == ends

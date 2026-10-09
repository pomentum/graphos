"""DB tests: ``/expand`` scoped to communities stays inside them in every direction.

The scope is read from columns the community write-back denormalises onto
each edge: the community of its end (``end_community_id``), for walking it
forwards, and of its start (``start_community_id``), for walking it
backwards. Walking backwards used to check the end, which is the node
already reached, so a scoped walk could step out through incoming edges.
"""

from __future__ import annotations

from http import HTTPStatus

import asyncpg
import httpx
import pytest

from graphos import writes
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_expand_scope"
COMMUNITY = {"a1": 1, "a2": 1, "a3": 1, "b1": 2, "b2": 2}


async def _seed(pool: asyncpg.Pool) -> None:
    """Community 1: a1 -> a2 -> a3. Community 2: b1, b2. Across: b1 -> a1, a2 -> b2."""
    await register_tenant(pool, TenantSpec(
        name=TENANT, vertex_labels=("Node",), edge_labels=("LINKS",),
    ))
    async with pool.acquire() as conn:
        for table in ("LINKS", "Node"):
            await conn.execute(f'DELETE FROM "{TENANT}"."{table}";')
    await writes.upsert_nodes(pool, tenant=TENANT, label="Node", rows=[
        {"id": node, "props": {}} for node in COMMUNITY
    ])
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Node", dst_label="Node", edge_type="LINKS",
        rows=[{"src_id": s, "dst_id": d, "props": {}}
              for s, d in (("a1", "a2"), ("a2", "a3"), ("b1", "a1"), ("a2", "b2"), ("b2", "b1"))],
    )
    rows = await pool.fetch(
        f"SELECT (id::text)::bigint AS gid, "
        f"       trim(both '\"' from (properties->'\"id\"')::text) AS ext "
        f'FROM "{TENANT}"."Node";',
    )
    gids = {r["ext"]: int(r["gid"]) for r in rows}
    # What a community recompute writes: the nodes, then both ends of every edge.
    await writes.set_node_communities(
        pool, tenant=TENANT, assignments=[(gids[n], c) for n, c in COMMUNITY.items()],
    )
    await writes.set_edge_end_communities(
        pool, tenant=TENANT, assignments=((gids[n], str(c)) for n, c in COMMUNITY.items()),
    )


def _client(pool: asyncpg.Pool) -> httpx.AsyncClient:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = pool
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://test/v1/{TENANT}/graph",
    )


async def _reached(api: httpx.AsyncClient, direction: str, scoped: bool) -> set[str]:
    params: dict[str, object] = {"node_id": "a1", "max_hops": 3, "direction": direction}
    if scoped:
        params["communities"] = ["1"]
    resp = await api.get("/expand", params=params)
    assert resp.status_code == HTTPStatus.OK, resp.text
    return {path["end_id"] for path in resp.json()["paths"]}


@requires_db
@pytest.mark.asyncio
async def test_a_scoped_walk_stays_in_its_communities_both_ways(
    db_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two vertices per write-back statement: five vertices span three chunks.
    monkeypatch.setattr(writes, "EDGE_COMMUNITY_CHUNK", 2)
    await _seed(db_pool)
    try:
        async with _client(db_pool) as api:
            # Unscoped, the walk leaves community 1 both ways.
            assert await _reached(api, "both", scoped=False) == {"a2", "a3", "b1", "b2"}
            # Scoped to community 1: forwards, backwards and both stay inside it.
            assert await _reached(api, "out", scoped=True) == {"a2", "a3"}
            assert await _reached(api, "in", scoped=True) == set()        # b1 -> a1 leads out
            assert await _reached(api, "both", scoped=True) == {"a2", "a3"}
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(f"SELECT drop_graph('{TENANT}', true);")

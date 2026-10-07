"""The community map: summarised from the clustered edges, stored, served.

The summary is checked on hand-made edges; the whole path (recompute, store,
``GET …/communities/map``) on a small graph of two clusters, which needs the
optional ``louvain`` extra (python-igraph).
"""

from __future__ import annotations

from http import HTTPStatus

import asyncpg
import httpx
import pytest

from graphos import community_map, louvain, writes
from graphos import louvain_compute as lc
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_community_map"
FUNCTION, CLASS = 3, 4   # label ids, as AGE puts them in the top 16 bits of a graph id


def _gid(label_id: int, n: int) -> int:
    return (label_id << 48) | n


def test_summary_counts_sizes_labels_hubs_and_links() -> None:
    a = [_gid(FUNCTION, i) for i in range(3)] + [_gid(CLASS, 9)]   # community 7
    b = [_gid(FUNCTION, 10 + i) for i in range(2)]                 # community 8
    loner = _gid(FUNCTION, 99)                                      # in no community
    edges = lc.EdgeArrays(rel_names=["CALLS", "USES"])
    for src, dst, rel in [
        (a[0], a[1], 0), (a[0], a[2], 0), (a[0], a[3], 1),   # a[0] is a's hub (3 edges)
        (b[0], b[1], 0),
        (a[1], b[0], 0), (a[2], b[0], 1),                    # two edges a -> b
        (b[1], a[0], 0),                                     # one edge b -> a
        (loner, a[0], 0),                                    # counts for the hub only
    ]:
        edges.src.append(src)
        edges.dst.append(dst)
        edges.rel.append(rel)
    assignments = {gid: 0 for gid in a} | {gid: 1 for gid in b}
    data = community_map.summarize(edges, assignments, {0: 7, 1: 8})

    first, second = data.communities[7], data.communities[8]
    assert (first.size, second.size) == (4, 2)
    assert first.labels == {FUNCTION: 3, CLASS: 1}
    assert (first.hub_gid, first.hub_degree) == (a[0], 5)
    assert (first.internal_edges, second.internal_edges) == (3, 1)
    assert data.links == {(7, 8, "CALLS"): 1, (7, 8, "USES"): 1, (8, 7, "CALLS"): 1}


@requires_db
@pytest.mark.asyncio
@pytest.mark.skipif(lc.ig is None, reason="louvain extra not installed")
async def test_recompute_stores_a_map_that_the_endpoint_serves(db_pool: asyncpg.Pool) -> None:
    await register_tenant(db_pool, TenantSpec(
        name=TENANT, vertex_labels=("Function",), edge_labels=("CALLS",),
    ))
    async with db_pool.acquire() as conn:
        for t in ("CALLS", "Function"):
            await conn.execute(f'DELETE FROM "{TENANT}"."{t}";')
        await conn.execute("DELETE FROM graphos_meta.community_map_runs WHERE tenant = $1", TENANT)
    # Two cliques of six joined by one edge.
    names = [f"{side}{i}" for side in "pq" for i in range(6)]
    await writes.upsert_nodes(db_pool, tenant=TENANT, label="Function", rows=[
        {"id": n, "props": {"name": n}} for n in names
    ])
    rows = [
        {"src_id": f"{side}{i}", "dst_id": f"{side}{j}", "props": {}}
        for side in "pq" for i in range(6) for j in range(i + 1, 6)
    ] + [{"src_id": "p0", "dst_id": "q0", "props": {}}]
    await writes.upsert_edges(
        db_pool, tenant=TENANT, src_label="Function", dst_label="Function",
        edge_type="CALLS", rows=rows,
    )

    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    transport = httpx.ASGITransport(app=app)
    base_url = f"http://test/v1/{TENANT}/graph"
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as api:
        before = (await api.get("/communities/map")).json()
        assert before["available"] is False

        await louvain.recompute_communities(db_pool, tenant=TENANT, min_community_size=2)
        resp = await api.get("/communities/map")
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["available"] is True and body["n_communities"] == 2
    sizes = sorted(c["size"] for c in body["communities"])
    assert sizes == [6, 6]
    for community in body["communities"]:
        assert community["labels"] == {"Function": 6}
        assert community["internal_edges"] == 15
        assert community["hub"]["label"] == "Function"
    # The bridge: p0 and q0 are their communities' hubs (6 edges each).
    assert {c["hub"]["id"] for c in body["communities"]} == {"p0", "q0"}
    links = [(link["count"], link["types"]) for link in body["links"]]
    assert links == [(1, [{"type": "CALLS", "count": 1}])]

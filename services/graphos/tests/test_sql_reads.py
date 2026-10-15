"""Reads that used to run Cypher, checked against the Cypher they replaced.

DB-gated. ``nodes/aggregate``, ``export`` and community member lists are
SQL now (AGE's Cypher executor was seen ignoring ``statement_timeout`` on
large graphs); each test runs the old Cypher through ``query_cypher`` as
the reference.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import asyncpg
import pytest

from graphos import writes
from graphos.api.communities import list_communities
from graphos.api.graph import aggregate_nodes, export_graph
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.contracts.graph import NodesAggregateRequest
from graphos.cypher import query_cypher
from graphos.identity import Principal

from .conftest import requires_db

T = "graphos_test_sql_reads"
ADMIN = Principal.static("test", admin=True)


def _request(pool: asyncpg.Pool) -> SimpleNamespace:
    # ``scope``: what the audit marks a read in (``audit_this``).
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)), scope={})


async def _seed(pool: asyncpg.Pool) -> None:
    await register_tenant(pool, TenantSpec(
        name=T, vertex_labels=("Document", "Collection"), edge_labels=("CITES", "PART_OF"),
    ))
    async with pool.acquire() as conn:
        for t in ("CITES", "PART_OF", "Document", "Collection"):
            await conn.execute(f'DELETE FROM "{T}"."{t}";')
    teams = ["red", "red", "red", "blue", "blue", 5, ["red", "blue"], None, "red", "green"]
    await writes.upsert_nodes(pool, tenant=T, label="Document", rows=[
        {"id": f"d{i}", "props": {
            **({"team": team} if team is not None else {}),
            "community_id": i % 3, "name": f"doc {i}",
        }}
        for i, team in enumerate(teams)
    ])
    await writes.upsert_nodes(pool, tenant=T, label="Collection", rows=[
        {"id": "c0", "props": {"team": "red", "community_id": 0}},
        {"id": "c1", "props": {"team": "blue"}},
    ])
    await writes.upsert_edges(
        pool, tenant=T, src_label="Document", dst_label="Document", edge_type="CITES",
        rows=[{"src_id": f"d{i}", "dst_id": f"d{(i + 1) % 10}", "props": {"w": i}}
              for i in range(10)] + [{"src_id": "d2", "dst_id": "d2", "props": {}}],
    )
    await writes.upsert_edges(
        pool, tenant=T, src_label="Document", dst_label="Collection", edge_type="PART_OF",
        rows=[{"src_id": f"d{i}", "dst_id": "c0", "props": {}} for i in range(5)],
    )


async def _cypher(pool: asyncpg.Pool, cypher: str, cols: list[str], **params) -> list[dict]:
    return await query_cypher(
        pool, graph=T, cypher=cypher, return_cols=[(c, "agtype") for c in cols], params=params,
    )


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(("label", "values"), [
    ("Document", None), ("Document", ["red", "5"]), ("Collection", None),
])
async def test_nodes_aggregate_matches_cypher(db_pool: asyncpg.Pool, label, values) -> None:
    await _seed(db_pool)
    where = "n.team IS NOT NULL" + (" AND n.team IN $values" if values else "")
    ref = await _cypher(
        db_pool, f"MATCH (n:{label}) WHERE {where} RETURN n.team AS v, count(n) AS c",
        ["v", "c"], **({"values": values} if values else {}),
    )
    resp = await aggregate_nodes(
        T, NodesAggregateRequest(label=label, group_by="team", values=values, limit=100),
        _request(db_pool), ADMIN,
    )
    got = [(g.value, g.count) for g in resp.groups]
    assert sorted(got) == sorted((str(r["v"]), int(r["c"])) for r in ref)
    assert [c for _, c in got] == sorted((c for _, c in got), reverse=True)


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(("node_ids", "rel_types"), [
    (["d0", "d1", "d2", "d3", "c0", "missing"], None),
    (["d0", "d1", "d2", "d3", "c0"], ["CITES"]),
    (None, None),
])
async def test_export_matches_cypher(db_pool: asyncpg.Pool, node_ids, rel_types) -> None:
    await _seed(db_pool)
    out = await export_graph(
        T, _request(db_pool), ADMIN, node_ids=node_ids, rel_types=rel_types, limit_nodes=1000,
    )
    every = await _cypher(db_pool, "MATCH (n) RETURN n.id AS id", ["id"])
    ids = node_ids or [r["id"] for r in every]
    ref_nodes = await _cypher(
        db_pool, "MATCH (n) WHERE n.id IN $ids RETURN n.id AS id, label(n) AS l",
        ["id", "l"], ids=ids,
    )
    assert {(n.key, n.attributes["_kind"]) for n in out.nodes} == {
        (str(r["id"]), str(r["l"])) for r in ref_nodes
    }
    rel = "AND type(r) IN $rels" if rel_types else ""
    ref_edges = await _cypher(
        db_pool,
        f"MATCH (a)-[r]->(b) WHERE a.id IN $ids AND b.id IN $ids {rel} "
        "RETURN id(r) AS rid, a.id AS s, b.id AS d, type(r) AS t",
        ["rid", "s", "d", "t"], ids=ids, **({"rels": rel_types} if rel_types else {}),
    )
    assert {(e.key, e.source, e.target, e.attributes["_type"]) for e in out.edges} == {
        (f"e_{r['rid']}", str(r["s"]), str(r["d"]), str(r["t"])) for r in ref_edges
    }
    assert any(e.source == e.target for e in out.edges) == any(
        r["s"] == r["d"] for r in ref_edges
    )


@requires_db
@pytest.mark.asyncio
async def test_community_members_match_cypher(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    resp = await list_communities(
        T, _request(db_pool), ADMIN, min_size=1, include_members=True, limit=0,
    )
    assert resp.communities
    for row in resp.communities:
        ref = await _cypher(
            db_pool, "MATCH (n) WHERE n.community_id = $cid AND n.id IS NOT NULL RETURN n.id AS id",
            ["id"], cid=row.community_id,
        )
        assert sorted(row.member_ids) == sorted(str(r["id"]) for r in ref)
    without = await list_communities(
        T, _request(db_pool), ADMIN, min_size=1, include_members=False, limit=0,
    )
    assert all(r.member_ids is None for r in without.communities)


async def _aggregate_reference(pool: asyncpg.Pool, req) -> tuple[list[tuple], bool]:
    """Group every edge in Python: what /edges/aggregate must return."""
    from graphos.api.graph import _agtype_text_value

    rels = req.rel_types or ["CITES", "PART_OF"]
    rows = []
    async with pool.acquire() as conn:
        for rel in rels:
            rows += [(rel, r) for r in await conn.fetch(
                f"""SELECT (s.properties -> '"{req.group_by_src}"'::agtype)::text AS s,
                           (d.properties -> '"{req.group_by_dst}"'::agtype)::text AS d,
                           (s.properties::text)::jsonb -> '{req.group_by_src}' AS sj,
                           (d.properties::text)::jsonb -> '{req.group_by_dst}' AS dj
                    FROM "{T}"."{rel}" e
                    JOIN "{T}"."_ag_label_vertex" s ON s.id = e.start_id
                    JOIN "{T}"."_ag_label_vertex" d ON d.id = e.end_id""")]
    counts: dict[tuple, int] = {}
    for rel, r in rows:
        if r["sj"] is None or r["dj"] is None:
            continue
        s_val, d_val = json.loads(r["sj"]), json.loads(r["dj"])
        if req.src_values and s_val not in req.src_values:   # strings only match strings
            continue
        if req.dst_values and d_val not in req.dst_values:
            continue
        if req.require_different and s_val == d_val:
            continue
        key = (_agtype_text_value(r["s"]), _agtype_text_value(r["d"]), rel)
        counts[key] = counts.get(key, 0) + 1
    groups = sorted(((*k, n) for k, n in counts.items()),
                    key=lambda g: (-g[3], g[0], g[1], g[2]))
    return groups[: req.limit], len(groups) > req.limit


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {},
    {"require_different": True},
    {"src_values": ["red", "5"]},
    {"dst_values": ["blue"], "rel_types": ["CITES"]},
    {"limit": 2},
    {"limit": 1, "require_different": True},
])
async def test_edges_aggregate_matches_reference(db_pool: asyncpg.Pool, options) -> None:
    from graphos.api.graph import aggregate_edges
    from graphos.contracts.graph import EdgesAggregateRequest

    await _seed(db_pool)
    req = EdgesAggregateRequest(group_by_src="team", group_by_dst="team", **options)
    expected, more = await _aggregate_reference(db_pool, req)
    resp = await aggregate_edges(T, req, _request(db_pool), ADMIN)
    assert [(g.src_value, g.dst_value, g.rel_type, g.count) for g in resp.groups] == expected
    assert resp.truncated == more

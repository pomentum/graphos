"""The graph map: what a large tenant holds, before looking at any one node.

  GET /v1/{tenant}/graph/map               labels, and edge counts between them
  GET /v1/{tenant}/graph/map/hubs?label=   a label's best-connected nodes
  GET /v1/{tenant}/graph/communities/map   the communities and the edges between them

A client draws one bubble per label and one line per (label, type, label)
link, then opens the hubs of a label as a starting point. Both read AGE's
storage directly: a node's label is the table it lives in, and an edge's
endpoints carry their label in the graph id (``_extract_label_id``), so the
links are counted per edge table without joining any vertex table.

Large edge tables (over ``SAMPLE_ABOVE`` rows by the planner's estimate) are
read through ``TABLESAMPLE SYSTEM`` and the counts scaled up: about one
second instead of a full scan of every edge (seven on a tenant of ten million
edges). Such counts are flagged ``estimated``; a link with only a few edges
inside a very large table can be missed. ``exact=true`` reads everything.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, status

from graphos.api.graph import _check_tenant, _safe_decode_props
from graphos.auth import ReadPrincipal, Tenant
from graphos.contracts.graph_map import (
    CommunityHub,
    CommunityLink,
    CommunityMapResponse,
    CommunityNode,
    GraphMapResponse,
    HubNode,
    HubsResponse,
    MapLabel,
    MapLink,
    MapLinkType,
)

router = APIRouter(prefix="/{tenant}/graph")

# Edge tables estimated larger than this are sampled unless `exact=true`.
SAMPLE_ABOVE = 300_000
# About this many rows are read from a sampled table.
SAMPLE_ROWS = 200_000
# Tables read at once: enough to overlap I/O, few enough to leave the pool to other requests.
PARALLEL_READS = 6
DEFAULT_HUBS = 40
MAX_HUBS = 200


@dataclass(frozen=True)
class _Table:
    label_id: int
    name: str
    kind: str          # "v" or "e"
    relation: str      # schema-qualified, quoted: safe to interpolate
    estimate: int      # the planner's row estimate (-1 when never analysed)


async def _tables(conn: asyncpg.Connection, tenant: str) -> list[_Table]:
    rows = await conn.fetch(
        """
        SELECT l.id, l.name, l.kind::text AS kind, l.relation::regclass::text AS relation,
               c.reltuples::bigint AS estimate
        FROM ag_catalog.ag_label l
        JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
        JOIN pg_class c ON c.oid = l.relation
        WHERE g.name = $1 AND l.name NOT IN ('_ag_label_vertex', '_ag_label_edge')
        """,
        tenant,
    )
    return [
        _Table(r["id"], r["name"], r["kind"], r["relation"], r["estimate"]) for r in rows
    ]


def _sample(table: _Table, exact: bool) -> tuple[str, float]:
    """The ``TABLESAMPLE`` clause for a table and the factor its counts are scaled by."""
    if exact or table.estimate <= SAMPLE_ABOVE:
        return "", 1.0
    percent = max(0.1, min(100.0, 100.0 * SAMPLE_ROWS / table.estimate))
    return f" TABLESAMPLE SYSTEM ({percent:.4f})", 100.0 / percent


_Job = Callable[[asyncpg.Connection], Awaitable[Any]]


async def _gather(pool: asyncpg.Pool, jobs: Sequence[_Job]) -> list[Any]:
    """Runs `jobs` (coroutine factories taking a connection), a few at a time."""
    gate = asyncio.Semaphore(PARALLEL_READS)

    async def run(job: _Job) -> Any:
        async with gate, pool.acquire() as conn:
            return await job(conn)

    return await asyncio.gather(*(run(job) for job in jobs))


@router.get(
    "/map",
    response_model=GraphMapResponse,
    tags=["graph"],
    summary="Labels and the edge counts between them",
    description=(
        "One entry per vertex label with its node count, and one per "
        "(source label, edge type, target label) with its edge count: the "
        "tenant at a glance, for an overview before any one node. Large "
        "edge tables are sampled and their counts estimated (flagged); "
        "`exact=true` counts every edge, which takes seconds on a tenant "
        "of millions of edges."
    ),
)
async def graph_map(
    tenant: Tenant,
    request: Request,
    principal: ReadPrincipal,
    exact: Annotated[
        bool, Query(description="Count every edge instead of sampling large tables."),
    ] = False,
) -> GraphMapResponse:
    _check_tenant(tenant, principal)
    started = time.perf_counter()
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        tables = await _tables(conn, tenant)
    if not tables:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no graph {tenant!r}")
    vertex = {t.label_id: t.name for t in tables if t.kind == "v"}

    def count_nodes(table: _Table) -> Callable[[asyncpg.Connection], Awaitable[MapLabel]]:
        async def job(conn: asyncpg.Connection) -> MapLabel:
            # ONLY: a parent label's own rows; its children are counted as themselves.
            n = await conn.fetchval(f"SELECT count(*) FROM ONLY {table.relation}")
            return MapLabel(label=table.name, count=n)
        return job

    def count_links(table: _Table) -> Callable[[asyncpg.Connection], Awaitable[list[MapLink]]]:
        sample, scale = _sample(table, exact)

        async def job(conn: asyncpg.Connection) -> list[MapLink]:
            rows = await conn.fetch(
                f"""
                SELECT ag_catalog._extract_label_id(start_id)::int AS src,
                       ag_catalog._extract_label_id(end_id)::int   AS dst,
                       count(*) AS n
                FROM ONLY {table.relation}{sample}
                GROUP BY 1, 2
                """,
            )
            return [
                MapLink(
                    src_label=vertex.get(r["src"], str(r["src"])),
                    type=table.name,
                    dst_label=vertex.get(r["dst"], str(r["dst"])),
                    count=round(r["n"] * scale),
                    estimated=scale != 1.0,
                )
                for r in rows
            ]
        return job

    vertices = [t for t in tables if t.kind == "v"]
    edges = [t for t in tables if t.kind == "e"]
    jobs = [count_nodes(t) for t in vertices] + [count_links(t) for t in edges]
    results = await _gather(pool, jobs)
    labels = sorted(
        (r for r in results[: len(vertices)] if r.count > 0), key=lambda r: (-r.count, r.label),
    )
    links = sorted(
        (link for batch in results[len(vertices):] for link in batch),
        key=lambda r: (-r.count, r.src_label, r.type, r.dst_label),
    )
    return GraphMapResponse(
        tenant=tenant,
        labels=labels,
        links=links,
        estimated=any(link.estimated for link in links),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


@router.get(
    "/map/hubs",
    response_model=HubsResponse,
    tags=["graph"],
    summary="A label's best-connected nodes",
    description=(
        "The nodes of `label` with the most edges (in and out, every type), "
        "most connected first: where to start exploring a label. Degrees "
        "from large edge tables are estimated from a sample (flagged)."
    ),
)
async def graph_hubs(
    tenant: Tenant,
    request: Request,
    principal: ReadPrincipal,
    label: Annotated[str, Query(description="A vertex label of the tenant.")],
    limit: Annotated[
        int, Query(ge=1, le=MAX_HUBS, description="How many nodes to return."),
    ] = DEFAULT_HUBS,
) -> HubsResponse:
    _check_tenant(tenant, principal)
    started = time.perf_counter()
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        tables = await _tables(conn, tenant)
    target = next((t for t in tables if t.kind == "v" and t.name == label), None)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no vertex label {label!r} in {tenant!r}",
        )

    def degrees(
        table: _Table,
    ) -> Callable[[asyncpg.Connection], Awaitable[list[tuple[str, float, bool]]]]:
        sample, scale = _sample(table, exact=False)

        async def job(conn: asyncpg.Connection) -> list[tuple[str, float, bool]]:
            rows = await conn.fetch(
                f"""
                SELECT g::text AS gid, count(*) AS n
                FROM ONLY {table.relation}{sample},
                     LATERAL (VALUES (start_id), (end_id)) AS ends(g)
                WHERE ag_catalog._extract_label_id(g)::int = $1
                GROUP BY g
                ORDER BY n DESC
                LIMIT $2
                """,
                target.label_id, limit * 4,
            )
            return [(r["gid"], r["n"] * scale, scale != 1.0) for r in rows]
        return job

    edges = [t for t in tables if t.kind == "e"]
    totals: dict[str, float] = {}
    estimated: dict[str, bool] = {}
    for batch in await _gather(pool, [degrees(t) for t in edges]):
        for gid, n, est in batch:
            totals[gid] = totals.get(gid, 0.0) + n
            estimated[gid] = estimated.get(gid, False) or est
    top = sorted(totals, key=lambda gid: (-totals[gid], gid))[:limit]

    nodes: list[HubNode] = []
    if top:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT id::text AS gid, properties::text AS props FROM ONLY {target.relation} "
                "WHERE id = ANY($1::text[]::ag_catalog.graphid[])",
                top,
            )
        props = {r["gid"]: _safe_decode_props(r["props"]) for r in rows}
        for gid in top:
            properties = props.get(gid)
            if properties is None:
                continue
            nodes.append(HubNode(
                id=str(properties.get("id", gid)),
                label=label,
                properties=properties,
                degree=round(totals[gid]),
                estimated=estimated[gid],
            ))
    return HubsResponse(
        tenant=tenant, label=label, nodes=nodes,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


DEFAULT_COMMUNITIES = 150
MAX_COMMUNITIES = 1000
LABELS_PER_COMMUNITY = 5


@router.get(
    "/communities/map",
    response_model=CommunityMapResponse,
    tags=["communities"],
    summary="The communities and the edges between them",
    description=(
        "The communities of the last community recompute, largest first: each "
        "with its size, its members per label, its best-connected member "
        "and its internal edge count, plus the edges between them per "
        "relationship type. Stored by the recompute from the edges it "
        "clustered, so it is exact and cheap, and as current as the last "
        "recompute (`computed_at`). `available` is false until a recompute "
        "has run since this was added."
    ),
)
async def community_map(
    tenant: Tenant,
    request: Request,
    principal: ReadPrincipal,
    limit: Annotated[
        int, Query(ge=1, le=MAX_COMMUNITIES, description="How many communities to return."),
    ] = DEFAULT_COMMUNITIES,
) -> CommunityMapResponse:
    _check_tenant(tenant, principal)
    async with request.app.state.pool.acquire() as conn:
        run = await conn.fetchrow(
            "SELECT computed_at, communities, modularity "
            "FROM graphos_meta.community_map_runs WHERE tenant = $1",
            tenant,
        )
        if run is None:
            return CommunityMapResponse(tenant=tenant, available=False)
        rows = await conn.fetch(
            """
            SELECT community_id, size, labels::text AS labels, hub_gid, hub_degree, internal_edges
            FROM graphos_meta.community_map WHERE tenant = $1
            ORDER BY size DESC, community_id
            LIMIT $2
            """,
            tenant, limit,
        )
        ids = [r["community_id"] for r in rows]
        link_rows = await conn.fetch(
            """
            SELECT src, dst, type, count FROM graphos_meta.community_links
            WHERE tenant = $1 AND src = ANY($2::int[]) AND dst = ANY($2::int[])
            ORDER BY count DESC
            """,
            tenant, ids,
        )
        hub_gids = [str(r["hub_gid"]) for r in rows if r["hub_gid"] is not None]
        hubs = {
            int(r["gid"]): r
            for r in await conn.fetch(
                f"""
                SELECT v.id::text AS gid, c.relname AS label, v.properties::text AS props
                FROM "{tenant}"."_ag_label_vertex" v
                JOIN pg_class c ON c.oid = v.tableoid
                WHERE v.id = ANY($1::text[]::ag_catalog.graphid[])
                """,
                hub_gids,
            )
        } if hub_gids else {}

    communities = []
    for r in rows:
        labels = json.loads(r["labels"])
        hub_row = hubs.get(r["hub_gid"]) if r["hub_gid"] is not None else None
        hub = None
        if hub_row is not None:
            properties = _safe_decode_props(hub_row["props"])
            hub = CommunityHub(
                id=str(properties.get("id", hub_row["gid"])),
                label=hub_row["label"],
                properties=properties,
                degree=r["hub_degree"],
            )
        communities.append(CommunityNode(
            community_id=r["community_id"],
            size=r["size"],
            labels=dict(list(labels.items())[:LABELS_PER_COMMUNITY]),
            hub=hub,
            internal_edges=r["internal_edges"],
        ))

    pairs: dict[tuple[int, int], CommunityLink] = {}
    for r in link_rows:
        key = (r["src"], r["dst"])
        link = pairs.get(key)
        if link is None:
            link = pairs[key] = CommunityLink(src=r["src"], dst=r["dst"], count=0, types=[])
        link.count += r["count"]
        link.types.append(MapLinkType(type=r["type"], count=r["count"]))
    return CommunityMapResponse(
        tenant=tenant,
        available=True,
        computed_at=run["computed_at"],
        modularity=run["modularity"],
        n_communities=run["communities"],
        communities=communities,
        links=sorted(pairs.values(), key=lambda link: -link.count),
    )

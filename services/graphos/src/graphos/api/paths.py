"""``POST /v1/{tenant}/graph/paths``: shortest paths between two nodes.

The search itself is :mod:`graphos.paths`; this module gives it one SQL
statement per level and turns the graphids it returns into nodes and edges.
Every statement runs under the pool's ``statement_timeout``.

Resolving the vertices of the paths found reads each one by graphid from
its own label table (AGE puts the label id in the graphid's top 16 bits).
AGE does not index vertex graphids, so that is one scan of each label on
the paths, unless ``GRAPHOS_VERTEX_GRAPHID_INDEXES`` is on; edges are read
through their graphid index.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request, status

from graphos import lookup, paths
from graphos.api.graph import _check_tenant, _check_vertex_label, _node_ambiguous
from graphos.auth import ReadPrincipal, Tenant
from graphos.bootstrap import registered_tenants
from graphos.contracts.paths import (
    MAX_PATH_LEVEL_EDGES,
    GraphPath,
    PathEdge,
    PathNode,
    PathsRequest,
    PathsResponse,
)

router = APIRouter(tags=["graph"], prefix="/{tenant}/graph")

# AGE graphid: label id in the top 16 bits, entry id in the low 48.
_ENTRY_BITS = 48
_GIDS = "ARRAY(SELECT x::graphid FROM unnest($1::text[]) AS x)"


@router.post(
    "/paths",
    response_model=PathsResponse,
    summary="Shortest paths between two nodes",
    description=(
        "Bidirectional breadth-first search, one batched SQL statement per level.  "
        "Returns up to `limit` paths, all of the shortest length found within "
        "`max_hops`; `hops` is null when there is none.  A level that would touch "
        f"more than {MAX_PATH_LEVEL_EDGES:,} edges stops the search (`truncated`)."
    ),
    responses={
        400: {"description": (
            "`src_label`, `dst_label` or an edge type in `rel_types` is not registered for the "
            "tenant."
        )},
        404: {"description": "`src` or `dst` does not exist."},
        409: {"description": "`src` or `dst` is in several labels; pass its label."},
    },
)
async def find_paths(
    tenant: Tenant,
    body: PathsRequest,
    request: Request,
    principal: ReadPrincipal,
) -> PathsResponse:
    _check_tenant(tenant, principal)
    _check_vertex_label(tenant, body.src_label)
    _check_vertex_label(tenant, body.dst_label)
    edge_labels = registered_tenants()[tenant].edge_labels
    unknown = sorted(set(body.rel_types or ()) - set(edge_labels))
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"edge types {unknown} are not registered for tenant '{tenant}'",
        )
    started = time.perf_counter()

    async with request.app.state.pool.acquire() as conn:
        src = await _endpoint(conn, tenant, body.src, body.src_label)
        dst = await _endpoint(conn, tenant, body.dst, body.dst_label)
        edge_types = await _edge_type_names(conn, tenant)
        source = _edge_source(tenant, body.rel_types)

        async def expand(frontier: list[int], walk: paths.Walk, cap: int) -> list[paths.Step]:
            rows = await conn.fetch(_expand_sql(source, walk), [str(g) for g in frontier], cap + 1)
            return [
                paths.Step(
                    src=r["src"], dst=r["dst"],
                    edge=paths.Edge(r["gid"], edge_types[r["tableoid"]], r["s"], r["e"]),
                )
                for r in rows
            ]

        found = await paths.shortest_paths(
            int(src.gid), int(dst.gid), expand=expand, direction=body.direction,
            max_hops=body.max_hops, limit=body.limit, max_edges=MAX_PATH_LEVEL_EDGES,
        )
        vertices = await _vertices(conn, tenant, found.paths)
        edge_props = (
            await _edge_properties(conn, tenant, found.paths) if body.include_properties else {}
        )

    def node(gid: int) -> PathNode:
        label, props = vertices[gid]
        return PathNode(
            id=str(props.get("id")), label=label,
            properties=props if body.include_properties else None,
        )

    def edge(e: paths.Edge) -> PathEdge:
        return PathEdge(
            type=e.type,
            src_id=str(vertices[e.start][1].get("id")),
            dst_id=str(vertices[e.end][1].get("id")),
            properties=edge_props.get(e.gid) if body.include_properties else None,
        )

    return PathsResponse(
        tenant=tenant,
        src=body.src,
        dst=body.dst,
        direction=body.direction,
        hops=found.hops,
        paths=[
            GraphPath(
                hops=len(p.edges),
                nodes=[node(v) for v in p.vertices],
                edges=[edge(e) for e in p.edges],
            )
            for p in found.paths
        ],
        truncated=found.truncated,
        visited=found.visited,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


async def _endpoint(
    conn: asyncpg.Connection, tenant: str, external_id: str, label: str | None,
) -> lookup.NodeRow:
    try:
        node = await lookup.find_node(conn, tenant=tenant, external_id=external_id, label=label)
    except lookup.AmbiguousMatchError as exc:
        raise _node_ambiguous(external_id, exc) from exc
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no node with id={external_id!r} in tenant {tenant!r}",
        )
    return node


async def _edge_type_names(conn: asyncpg.Connection, tenant: str) -> dict[int, str]:
    rows = await conn.fetch(
        "SELECT c.oid, c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_inherits i ON i.inhrelid = c.oid "
        "JOIN pg_class p ON p.oid = i.inhparent "
        "WHERE n.nspname = $1 AND p.relname = '_ag_label_edge';",
        tenant,
    )
    return {int(r["oid"]): str(r["relname"]) for r in rows}


def _edge_source(tenant: str, rel_types: list[str] | None) -> str:
    if not rel_types:
        return f'"{tenant}"."_ag_label_edge"'
    union = " UNION ALL ".join(
        f'SELECT id, start_id, end_id, tableoid FROM "{tenant}"."{t}"'
        for t in sorted(set(rel_types))
    )
    return f"({union})"


def _expand_sql(source: str, walk: paths.Walk) -> str:
    """Edges leaving the frontier ``$1`` (graphids as text), at most ``$2``."""
    cols = (
        "(e.id::text)::bigint AS gid, e.tableoid::bigint AS tableoid, "
        "(e.start_id::text)::bigint AS s, (e.end_id::text)::bigint AS e"
    )
    parts = []
    if walk in ("out", "both"):
        parts.append(
            f"SELECT (e.start_id::text)::bigint AS src, (e.end_id::text)::bigint AS dst, {cols} "
            f"FROM {source} e WHERE e.start_id = ANY({_GIDS})"
        )
    if walk in ("in", "both"):
        parts.append(
            f"SELECT (e.end_id::text)::bigint AS src, (e.start_id::text)::bigint AS dst, {cols} "
            f"FROM {source} e WHERE e.end_id = ANY({_GIDS})"
        )
    return f"SELECT * FROM ({' UNION ALL '.join(parts)}) steps LIMIT $2;"


async def _vertices(
    conn: asyncpg.Connection, tenant: str, found: list[paths.Path],
) -> dict[int, tuple[str, dict[str, Any]]]:
    """``graphid -> (label, properties)`` for every vertex on the paths."""
    gids = {v for p in found for v in p.vertices}
    if not gids:
        return {}
    labels = {
        int(r["id"]): str(r["name"])
        for r in await conn.fetch(
            "SELECT l.id, l.name FROM ag_catalog.ag_label l "
            "JOIN ag_catalog.ag_graph g ON g.graphid = l.graph "
            "WHERE g.name = $1 AND l.kind = 'v';",
            tenant,
        )
    }
    by_label: dict[str, list[int]] = defaultdict(list)
    for gid in gids:
        by_label[labels[gid >> _ENTRY_BITS]].append(gid)
    out: dict[int, tuple[str, dict[str, Any]]] = {}
    for label, members in by_label.items():
        for r in await conn.fetch(
            f"SELECT (id::text)::bigint AS gid, properties::text::jsonb AS props "
            f'FROM ONLY "{tenant}"."{label}" WHERE id = ANY({_GIDS});',
            [str(g) for g in members],
        ):
            out[int(r["gid"])] = (label, json.loads(r["props"]))
    return out


async def _edge_properties(
    conn: asyncpg.Connection, tenant: str, found: list[paths.Path],
) -> dict[int, dict[str, Any]]:
    by_type: dict[str, set[int]] = defaultdict(set)
    for p in found:
        for e in p.edges:
            by_type[e.type].add(e.gid)
    out: dict[int, dict[str, Any]] = {}
    for edge_type, gids in by_type.items():
        for r in await conn.fetch(
            f"SELECT (id::text)::bigint AS gid, properties::text::jsonb AS props "
            f'FROM ONLY "{tenant}"."{edge_type}" WHERE id = ANY({_GIDS});',
            [str(g) for g in gids],
        ):
            out[int(r["gid"])] = json.loads(r["props"])
    return out

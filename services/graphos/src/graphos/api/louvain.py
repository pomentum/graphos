"""Louvain admin endpoint."""

from __future__ import annotations

import time

import asyncpg
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from graphos.api.graph import _check_tenant
from graphos.api.jobs import ACCEPTED_RESPONSE, respond_async, wants_async
from graphos.api.tenants import Principal
from graphos.auth import require_admin
from graphos.contracts.louvain import (
    RecomputeCommunitiesRequest,
    RecomputeCommunitiesResponse,
)
from graphos.louvain import LOUVAIN_AVAILABLE, LOUVAIN_INSTALL_HINT, recompute_communities

router = APIRouter(tags=["communities"], prefix="/{tenant}")


@router.post(
    "/recompute-communities",
    response_model=RecomputeCommunitiesResponse,
    summary="Recompute Louvain community partitions for the tenant",
    description=(
        "Streams every semantic edge (optionally filtered by rel_types) into "
        "an in-process igraph, runs Louvain modularity optimisation, and "
        "writes `community_id` back to each vertex. Single-flight via a "
        "postgres advisory lock keyed on the tenant name — concurrent "
        "recomputes for the same tenant return 409. Admin-scoped. Returns "
        "501 when the optional `louvain` extra (python-igraph) is not "
        "installed. With `Prefer: respond-async` it runs as a background job "
        "(202, see `/v1/jobs`)."
    ),
    responses=ACCEPTED_RESPONSE,
)
async def recompute(
    tenant: str,
    body: RecomputeCommunitiesRequest,
    request: Request,
    principal: Principal,
) -> RecomputeCommunitiesResponse | JSONResponse:
    require_admin(principal)
    if not LOUVAIN_AVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=LOUVAIN_INSTALL_HINT,
        )
    _check_tenant(tenant, principal)
    if wants_async(request):
        return await respond_async(
            request, kind="recompute_communities", tenant=tenant, body=body,
            principal=principal,
        )
    return await run_recompute(request.app.state.pool, tenant, body)


async def run_recompute(
    pool: asyncpg.Pool, tenant: str, body: RecomputeCommunitiesRequest,
) -> RecomputeCommunitiesResponse:
    """The recompute itself, for the endpoint and for background jobs."""
    # Single-flight per tenant via advisory lock. We hash the tenant name
    # to fit in a bigint lock key.
    async with pool.acquire() as lock_conn:
        got = await lock_conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtext($1))",
            tenant,
        )
        if not got:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"another recompute is in progress for tenant '{tenant}'. "
                    "Retry once it completes."
                ),
            )
        try:
            started = time.perf_counter()
            result = await recompute_communities(
                pool,
                tenant=tenant,
                rel_types=body.rel_types,
                rel_weights=body.rel_weights,
                node_filter=body.node_filter,
                resolution=body.resolution,
                min_community_size=body.min_community_size,
                write=not body.dry_run,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
        finally:
            await lock_conn.execute(
                "SELECT pg_advisory_unlock(hashtext($1))",
                tenant,
            )

    return RecomputeCommunitiesResponse(
        tenant=tenant,
        n_nodes=result.n_nodes,
        n_edges_scanned=result.n_edges_scanned,
        n_communities=result.n_communities,
        n_assigned=result.n_assigned,
        n_singletons=result.n_singletons,
        largest_community_size=result.largest_community_size,
        modularity=result.modularity,
        edge_stream_ms=result.edge_stream_ms,
        igraph_ms=result.igraph_ms,
        writeback_ms=result.writeback_ms,
        elapsed_ms=elapsed_ms,
        dry_run=body.dry_run,
    )

"""Graph metrics endpoint: PageRank, degree, components, betweenness."""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from graphos import analysis
from graphos.api.graph import _check_tenant
from graphos.api.jobs import ACCEPTED_RESPONSE, respond_async, run_sync, wants_async
from graphos.api.louvain import busy
from graphos.auth import MaintainPrincipal, Tenant
from graphos.contracts.graph_metrics import ComputeMetricsRequest, ComputeMetricsResponse
from graphos.graph_metrics import run_metrics

router = APIRouter(tags=["metrics"], prefix="/{tenant}/graph")


@router.post(
    "/metrics:compute",
    response_model=ComputeMetricsResponse,
    summary="Compute graph metrics and store them on the nodes",
    description=(
        "Computes PageRank, degree, weakly connected components and/or betweenness over "
        "the tenant's edges (optionally only some types, or the subgraph a node_filter "
        "selects) and writes them onto each node as graphos_* properties, which are "
        "reserved: consumers cannot write them. Only values that changed are written, so a "
        "re-run on an unchanged graph writes nothing. Nodes with no edge of the chosen types "
        "do not take part. One whole-graph job per tenant at a time, shared with community "
        "recompute (409 while one runs). Needs `maintain` on the tenant. With "
        "`Prefer: respond-async` it runs "
        "as a background job (202, see `/v1/jobs`). Read the results with the nodes, or the "
        "top ones with `nodes:scan` and `order_by: [{\"property\": \"graphos_pagerank\", "
        "\"desc\": true}]`."
    ),
    responses=ACCEPTED_RESPONSE,
)
async def compute_metrics(
    tenant: Tenant,
    body: ComputeMetricsRequest,
    request: Request,
    principal: MaintainPrincipal,
) -> ComputeMetricsResponse | JSONResponse:
    _check_tenant(tenant, principal)
    if wants_async(request):
        return await respond_async(
            request, kind="compute_metrics", tenant=tenant, body=body, principal=principal,
        )
    return await run_sync(
        request, kind="compute_metrics", tenant=tenant, body=body, principal=principal,
        run=lambda: run_compute(request.app.state.pool, tenant, body),
    )


async def run_compute(
    pool: asyncpg.Pool, tenant: str, body: ComputeMetricsRequest,
) -> ComputeMetricsResponse:
    """The run itself, for the endpoint and for background jobs."""
    try:
        async with analysis.tenant_lock(pool, tenant):
            return await run_metrics(pool, tenant, body)
    except analysis.TenantBusyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=busy(tenant)) from None

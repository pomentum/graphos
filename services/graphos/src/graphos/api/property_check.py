"""``properties:check``: what a tenant's stored rows break of its property schema."""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from graphos.api.graph import _check_tenant
from graphos.api.jobs import ACCEPTED_RESPONSE, respond_async, run_sync, wants_async
from graphos.auth import MaintainPrincipal
from graphos.bootstrap import registered_tenants
from graphos.contracts.property_schema import PropertiesCheckRequest, PropertiesCheckResponse
from graphos.property_report import check_stored

router = APIRouter(tags=["properties"], prefix="/{tenant}/graph")


@router.post(
    "/properties:check",
    response_model=PropertiesCheckResponse,
    summary="Check the stored rows against the property schema",
    description=(
        "Reads every row of the chosen vertex labels and edge types (all by default) and "
        "checks it as a write would be, as if every rule applied whatever the mode: per label "
        "and type, the rows strict would refuse, counts per `<label or *>.<property>:<rule>`, "
        "some example ids per rule, and the property names the rows carry that no definition "
        "names. Writes nothing. Needs `maintain`. With `Prefer: respond-async` it runs as a "
        "background job (202, see `/v1/jobs`)."
    ),
    responses=ACCEPTED_RESPONSE,
)
async def check_properties(
    tenant: str,
    body: PropertiesCheckRequest,
    request: Request,
    principal: MaintainPrincipal,
) -> PropertiesCheckResponse | JSONResponse:
    _check_tenant(tenant, principal)
    if wants_async(request):
        return await respond_async(
            request, kind="check_properties", tenant=tenant, body=body, principal=principal,
        )
    return await run_sync(
        request, kind="check_properties", tenant=tenant, body=body, principal=principal,
        run=lambda: run_check(request.app.state.pool, tenant, body),
    )


async def run_check(
    pool: asyncpg.Pool, tenant: str, body: PropertiesCheckRequest,
) -> PropertiesCheckResponse:
    """The run itself, for the endpoint and for background jobs."""
    return await check_stored(pool, registered_tenants()[tenant], body)

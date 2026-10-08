"""Endpoints any token may call about itself and its tenants.

- ``GET /v1/me``                who is signed in: user, levels, rights
- ``GET /v1/{tenant}/schema``   one tenant's labels and search indexes

They give a client (such as the UI) what it needs to start without an admin
token: which tenants to offer, and which labels and search indexes they have.
The admin-only ``/v1/tenants`` endpoints keep the full spec (DDL, rules,
metadata).
"""

from __future__ import annotations

from typing import Literal, cast

from fastapi import APIRouter, HTTPException, status

from graphos.auth import ReadPrincipal
from graphos.bootstrap import registered_tenants
from graphos.contracts.tenants import EdgeEndpointSpec, MeResponse, TenantSchema

router = APIRouter(tags=["tenants"])


@router.get("/me", summary="Who is signed in: user, levels per tenant, rights")
async def me(principal: ReadPrincipal) -> MeResponse:
    tenants = principal.visible(registered_tenants())
    levels = {t: level.label for t in tenants if (level := principal.level(t)) is not None}
    return MeResponse(
        consumer=principal.consumer,
        kind=cast(Literal["person", "service"], principal.kind)
        if principal.subject else "anonymous",
        via=cast(Literal["password", "oidc"], principal.via) if principal.subject else "none",
        admin=principal.admin,
        must_change=principal.must_change,
        tenants=tenants,
        levels=levels,  # type: ignore[arg-type]
        create_tenants=["*"] if principal.admin else list(principal.create_tenants),
    )


@router.get(
    "/{tenant}/schema",
    summary="One tenant's labels, hierarchy, declared endpoints and search indexes",
    responses={404: {"description": "Tenant not registered."}},
)
async def tenant_schema(tenant: str, principal: ReadPrincipal) -> TenantSchema:  # noqa: ARG001 — auth
    spec = registered_tenants().get(tenant)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tenant '{tenant}' is not registered",
        )
    return TenantSchema(
        name=spec.name,
        vertex_labels=list(spec.vertex_labels),
        edge_labels=list(spec.edge_labels),
        label_hierarchy=dict(spec.label_hierarchy),
        search_indexes=dict(spec.search_indexes),
        # One entry per type, however many rules it has.
        rule_edge_labels=list(dict.fromkeys(edge for edge, _ in spec.resolve_rules)),
        edge_endpoints=_endpoints_by_type(spec.edge_endpoints),
        strict_endpoints=bool(spec.strict_endpoints),
    )


def _endpoints_by_type(
    endpoints: tuple[tuple[str, str, str], ...],
) -> dict[str, list[EdgeEndpointSpec]]:
    out: dict[str, list[EdgeEndpointSpec]] = {}
    for edge_type, src, dst in sorted(endpoints):
        out.setdefault(edge_type, []).append(EdgeEndpointSpec(src=src, dst=dst))
    return out

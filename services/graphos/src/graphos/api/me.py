"""Endpoints any token may call about itself and its tenants.

- ``GET /v1/me``                the calling token: consumer, access, tenants
- ``GET /v1/{tenant}/schema``   one tenant's labels and search indexes

They give a client (such as the UI) what it needs to start without an admin
token: which tenants to offer, and which labels and search indexes they have.
The admin-only ``/v1/tenants`` endpoints keep the full spec (DDL, rules,
metadata).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from graphos.auth import ReadPrincipal
from graphos.bootstrap import registered_tenants
from graphos.contracts.tenants import MeResponse, TenantSchema

router = APIRouter(tags=["tenants"])


@router.get("/me", summary="The calling token: consumer, access and tenants")
async def me(principal: ReadPrincipal) -> MeResponse:
    registered = registered_tenants()
    tenants = registered if principal.admin else {
        name: spec for name, spec in registered.items() if name in principal.tenants
    }
    return MeResponse(
        consumer=principal.consumer,
        admin=principal.admin,
        access=principal.access.value,
        tenants=sorted(tenants),
    )


@router.get(
    "/{tenant}/schema",
    summary="One tenant's labels, label hierarchy and search indexes",
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
        rule_edge_labels=[edge for edge, _ in spec.resolve_rules],
    )

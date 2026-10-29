"""Endpoints any token may call about itself and its tenants.

- ``GET /v1/me``                who is signed in: user, levels, rights, preferences
- ``PATCH /v1/me/preferences``  change the caller's web UI preferences
- ``GET /v1/{tenant}/schema``   one tenant's labels and search indexes

They give a client (such as the UI) what it needs to start without an admin
token: which tenants to offer, and which labels and search indexes they have.
The admin-only ``/v1/tenants`` endpoints keep the full spec (DDL, rules,
metadata).
"""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

import asyncpg
from fastapi import APIRouter, Body, HTTPException, Request, Response, status

from graphos import preferences, schema_version
from graphos.auth import ReadPrincipal, Tenant
from graphos.bootstrap import (
    DEFAULT_COMMUNITY_ALGORITHM,
    load_spec_versioned,
    registered_tenants,
)
from graphos.contracts.property_schema import PropertySchemaSpec
from graphos.contracts.tenants import (
    DeprecatedSpec,
    DisplaySpec,
    EdgeEndpointSpec,
    MeResponse,
    SchemaRename,
    TenantSchema,
)

log = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/me",
    tags=["auth"],
    summary="Who is signed in: user, levels per tenant, rights",
    description=(
        "Any valid token, even one whose password must change first. The caller's user name "
        "and kind, how it signed in, whether it is an admin or must change its password, the "
        "registered tenants it may use with its level on each, the patterns of tenant names "
        "it may create, and its web UI preferences. With authentication disabled, an "
        "anonymous admin."
    ),
)
async def me(principal: ReadPrincipal, request: Request) -> MeResponse:
    tenants = principal.visible(registered_tenants())
    prefs = await _preferences(request, principal.subject)
    levels = {t: level.label for t in tenants if (level := principal.level(t)) is not None}
    return MeResponse(
        consumer=principal.consumer,
        kind=cast(Literal["person", "service"], principal.kind)
        if principal.subject else "anonymous",
        via=cast(Literal["password", "oidc"], principal.via) if principal.subject else "none",
        admin=principal.admin,
        must_change=principal.must_change,
        tenants=tenants,
        levels=levels,
        create_tenants=["*"] if principal.admin else list(principal.create_tenants),
        preferences=prefs,
    )


async def _preferences(request: Request, subject: str | None) -> dict[str, Any]:
    """The caller's preferences; never in the way of signing in (empty when
    there is no user, or they cannot be read)."""
    pool = getattr(request.app.state, "pool", None)
    if not subject or pool is None:
        return {}
    try:
        return await preferences.load(pool, subject)
    except (asyncpg.PostgresError, OSError, ValueError):
        log.warning("preferences could not be read", exc_info=True)
        return {}


@router.patch(
    "/me/preferences",
    tags=["auth"],
    summary="Change the caller's web UI preferences (JSON Merge Patch)",
    description=(
        "Only the preferences graphos knows are kept, each with the values it takes: "
        "`theme` (`auto`, `light`, `dark`) and `last_tenant` (a tenant name). `null` "
        "removes one. Answers all of them."
    ),
    responses={
        400: {"description": "The caller is not a signed-in user: preferences belong to users."},
        422: {"description": "An unknown preference, or a value it does not take."},
    },
)
async def update_preferences(
    principal: ReadPrincipal,
    request: Request,
    patch: dict[str, Any] = Body(..., examples=[{"theme": "dark"}]),  # noqa: B008 — FastAPI idiom
) -> dict[str, Any]:
    if not principal.subject:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="preferences belong to a signed-in user",
        )
    try:
        return await preferences.update(request.app.state.pool, principal.subject, patch)
    except preferences.PreferenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc),
        ) from exc


@router.get(
    "/{tenant}/schema",
    tags=["tenants"],
    summary="One tenant's labels, hierarchy, declared endpoints and search indexes",
    description=(
        "`read` on the tenant. What a client needs to work with it without an admin token: "
        "its vertex and edge labels, the label hierarchy, the search indexes, the edge types "
        "that have resolve rules, the declared edge endpoints and whether they are strict, "
        "the display hints and the property schema (each definition with only the keywords it "
        "states). The whole schema, rules and metadata included, is "
        "`GET /v1/tenants/{name}/spec` (`maintain`). The `ETag` is the schema's version, for "
        "`If-Match` on schema writes: read with the schema, so it describes exactly this one."
    ),
    responses={404: {"description": "Tenant not registered."}},
    # Declarations as declared: a property definition lists only the keywords
    # it states, so a narrowing entry is not read as restating the defaults.
    response_model_exclude_unset=True,
)
async def tenant_schema(
    tenant: Tenant, request: Request, response: Response,
    principal: ReadPrincipal,  # noqa: ARG001 — auth
) -> TenantSchema:
    # The stored schema with its version, not this replica's copy: the ETag
    # must describe exactly what is answered (graphos.schema_version).
    found = await load_spec_versioned(request.app.state.pool, tenant)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tenant '{tenant}' is not registered",
        )
    spec, version = found
    response.headers["ETag"] = schema_version.etag(version)
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
        community_algorithm=spec.community_algorithm or DEFAULT_COMMUNITY_ALGORITHM,
        display=DisplaySpec.model_validate(spec.display),
        property_schema=PropertySchemaSpec.model_validate(spec.property_schema),
        deprecated=DeprecatedSpec.model_validate(spec.deprecated),
        renames=[SchemaRename.model_validate({"status": "done", **r}) for r in spec.renames],
    )


def _endpoints_by_type(
    endpoints: tuple[tuple[str, str, str], ...],
) -> dict[str, list[EdgeEndpointSpec]]:
    out: dict[str, list[EdgeEndpointSpec]] = {}
    for edge_type, src, dst in sorted(endpoints):
        out.setdefault(edge_type, []).append(EdgeEndpointSpec(src=src, dst=dst))
    return out

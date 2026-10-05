"""Tenant management endpoints.

Surface:

- ``GET  /v1/tenants``           list currently-registered tenants
- ``POST /v1/tenants``           register or re-apply a tenant spec (idempotent)
- ``GET  /v1/tenants/{name}``    inspect one tenant's registered spec

These endpoints require an admin-scoped token. Authentication is enforced by
the token store, not by URL path — there is no ``/admin`` URL prefix. The
typed routes are themselves the contract; admin scope is a token attribute,
not a path convention.

Consumers own their schemas. graphos accepts whatever spec a token-authorised
consumer registers and brings the AGE graph up to it. Registrations are
persisted in ``graphos_meta.tenants`` and reloaded on restart; re-registering
is idempotent and cheap. See ``bootstrap.py`` module docstring for context.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from graphos.auth import TokenPrincipal, require_admin, require_principal
from graphos.bootstrap import (
    TenantSpec,
    bootstrap_tenant,
    load_spec,
    persist_spec,
    persist_spec_replace,
    registered_tenants,
    remember,
    unregister_tenant,
)
from graphos.contracts.tenants import (
    RegisterResponse,
    TenantInfo,
    TenantSpecBody,
)
from graphos.resolver.catalogue import refresh_catalogue
from graphos.resolver.indexer import ensure_resolve_rule_indexes

log = logging.getLogger(__name__)

router = APIRouter(tags=["tenants"], prefix="/tenants")

Principal = Annotated[TokenPrincipal, Depends(require_principal)]


def _spec_to_info(spec: TenantSpec) -> TenantInfo:
    return TenantInfo(
        name=spec.name,
        vertex_labels=list(spec.vertex_labels),
        edge_labels=list(spec.edge_labels),
        n_indexes=len(spec.indexes),
        n_generated_columns=len(spec.generated_columns),
        n_resolve_rules=len(spec.resolve_rules),
        label_hierarchy={c: p for (c, p) in spec.label_hierarchy},
        meta=dict(spec.meta),
    )


@router.get("", summary="List registered tenants")
async def list_tenants(principal: Principal) -> dict[str, list[TenantInfo]]:
    require_admin(principal)
    return {"tenants": [_spec_to_info(s) for s in registered_tenants().values()]}


@router.get("/{name}", summary="Inspect one registered tenant")
async def get_tenant(name: str, principal: Principal) -> TenantInfo:
    require_admin(principal)
    specs = registered_tenants()
    if name not in specs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tenant '{name}' is not registered",
        )
    return _spec_to_info(specs[name])


@router.post("", summary="Register a tenant (idempotent)", response_model=RegisterResponse)
async def register(
    body: TenantSpecBody,
    request: Request,
    principal: Principal,
    force_shrink: bool = False,
) -> RegisterResponse:
    """Register or re-apply a tenant spec.

    By default ``persist_spec`` runs in **merge mode** — the persisted
    label sets are unioned with the incoming ones, never shrunk.  This
    is the safe default: a bare ``{"name": "<tenant>"}`` smoke probe
    or a partial-spec client can't silently demote the schema for
    in-flight workers that were relying on labels created earlier.

    ``?force_shrink=true`` opts into wholesale replacement — used when
    an operator intentionally wants to remove labels from a tenant
    (deprecation).  Both modes still call ``bootstrap_tenant`` first,
    so any new labels in the incoming spec get materialised.  Removed
    labels are NOT dropped from the AGE catalog (operators must drop
    those manually); only the persisted spec narrows.
    """
    require_admin(principal)

    # ``edge_labels`` arrives normalised to ``EdgeLabelSpec`` objects (the
    # contract coerces bare strings on input).  Split into the bootstrap
    # spec's two tuples: the plain edge-label names (deduped — AGE only
    # needs the label created once) and the per-edge resolve rules
    # (NOT deduped — multiple entries with the same ``name`` may each
    # carry an independent ``resolve_rule``, all kept so the resolver
    # endpoint can run every rule for that edge_type).
    seen_names: set[str] = set()
    edge_label_names: list[str] = []
    for e in body.edge_labels:
        if e.name not in seen_names:
            seen_names.add(e.name)
            edge_label_names.append(e.name)
    edge_label_names_t = tuple(edge_label_names)
    resolve_rules = tuple(
        (e.name, dict(e.resolve_rule))
        for e in body.edge_labels
        if e.resolve_rule is not None
    )

    spec = TenantSpec(
        name=body.name,
        vertex_labels=tuple(body.vertex_labels),
        edge_labels=edge_label_names_t,
        indexes=tuple(body.indexes),
        generated_columns=tuple(body.generated_columns),
        label_hierarchy=tuple(
            (child, parent) for child, parent in body.label_hierarchy.items()
        ),
        resolve_rules=resolve_rules,
        meta=dict(body.meta),
    )

    pool = request.app.state.pool
    try:
        await bootstrap_tenant(
            pool, spec,
            vertex_graphid_indexes=request.app.state.settings.vertex_graphid_indexes,
        )
    except ValueError as exc:
        # Spec-level problems found during bootstrap (e.g. a label_hierarchy
        # entry naming a label that is not in vertex_labels) are the caller's.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc),
        ) from exc
    # Merge-by-default; explicit force_shrink=true to wipe-and-replace
    # the persisted label sets (see endpoint docstring).
    if force_shrink:
        log.warning(
            "tenant register with force_shrink=true — persisted spec "
            "will be replaced wholesale, not merged",
            extra={"tenant": spec.name},
        )
        await persist_spec_replace(pool, spec)
    else:
        await persist_spec(pool, spec)
    # Persisting merges with the stored spec (unless force_shrink); keep the
    # merged result in memory so a partial re-registration never hides labels
    # or resolve rules registered earlier.
    spec = await load_spec(pool, spec.name) or spec
    remember(spec)

    # Warm the per-tenant property→labels catalogue so the first
    # ``edges:resolve`` call doesn't pay the discovery cost.  Best-effort:
    # if AGE / the tenant schema isn't fully wired (no node rows yet) the
    # catalogue stays empty and the resolver refreshes on demand.
    with contextlib.suppress(Exception):  # non-fatal warm-up
        await refresh_catalogue(pool, spec.name)

    # Auto-derive functional indexes for every ``resolve_rule`` block:
    # graphos owns the SQL — consumers declare intent only.  Idempotent
    # across registrations thanks to ``CREATE INDEX IF NOT EXISTS``.
    # Best-effort: an index failure on one label doesn't block the
    # registration response, just gets logged.
    n_resolver_indexes = 0
    try:
        ensured = await ensure_resolve_rule_indexes(
            pool, tenant=spec.name, spec=spec,
        )
        n_resolver_indexes = len(ensured)
        log.info(
            "resolver.indexes_ensured tenant=%r n_indexes=%d",
            spec.name, n_resolver_indexes,
        )
    except Exception:  # noqa: BLE001 — non-fatal post-bootstrap step
        log.exception(
            "resolver.indexer: failed to ensure indexes for tenant %r",
            spec.name,
        )

    return RegisterResponse(
        tenant=spec.name,
        vertex_labels=list(spec.vertex_labels),
        edge_labels=list(spec.edge_labels),
        n_indexes=len(spec.indexes),
        n_generated_columns=len(spec.generated_columns),
        n_inheritance_edges=len(spec.label_hierarchy),
        n_resolve_rules=len(spec.resolve_rules),
        n_resolver_indexes=n_resolver_indexes,
    )


@router.delete(
    "/{name}",
    summary="Forget a registered tenant (does NOT drop the AGE graph data)",
    description=(
        "Removes the registration row from `graphos_meta.tenants` and the "
        "in-process cache. The AGE graph itself stays intact — data-plane "
        "tombstones via `DELETE /v1/{tenant}/graph/nodes` if you also "
        "want to remove the rows. Re-POSTing the same spec brings the "
        "tenant back."
    ),
)
async def unregister(
    name: str,
    request: Request,
    principal: Principal,
) -> dict[str, object]:
    require_admin(principal)
    pool = request.app.state.pool
    existed = await unregister_tenant(pool, name)
    if not existed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tenant '{name}' is not registered",
        )
    return {"tenant": name, "status": "unregistered"}

"""Tenant management endpoints.

Surface:

- ``GET  /v1/tenants``           list currently-registered tenants
- ``POST /v1/tenants``           register or re-apply a tenant spec (idempotent)
- ``GET  /v1/tenants/{name}``    inspect one tenant's registered spec
- ``DELETE /v1/tenants/{name}/edge_endpoints``  remove one declared label pair

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

import logging

from fastapi import APIRouter, HTTPException, Request, status

from graphos.auth import ReadPrincipal, WritePrincipal, require_admin
from graphos.bootstrap import (
    TenantSpec,
    bootstrap_tenant,
    ensure_search_indexes,
    load_spec,
    persist_spec,
    persist_spec_replace,
    registered_tenants,
    remember,
    remove_edge_endpoint,
    unregister_tenant,
    validate_edge_endpoints,
)
from graphos.contracts.tenants import (
    RegisterResponse,
    TenantInfo,
    TenantSpecBody,
)
from graphos.resolver.indexer import ensure_resolve_rule_indexes

log = logging.getLogger(__name__)

router = APIRouter(tags=["tenants"], prefix="/tenants")


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
        search_indexes=dict(spec.search_indexes),
    )


@router.get("", summary="List registered tenants")
async def list_tenants(principal: ReadPrincipal) -> dict[str, list[TenantInfo]]:
    require_admin(principal)
    return {"tenants": [_spec_to_info(s) for s in registered_tenants().values()]}


@router.get("/{name}", summary="Inspect one registered tenant")
async def get_tenant(name: str, principal: ReadPrincipal) -> TenantInfo:
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
    principal: WritePrincipal,
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
    edge_endpoints = tuple(
        (edge_type, pair.src, pair.dst)
        for edge_type, pairs in body.edge_endpoints.items()
        for pair in pairs
    )
    # Declared pairs may name labels and types registered earlier, or added now.
    known = registered_tenants().get(body.name)
    try:
        validate_edge_endpoints(
            edge_endpoints,
            edge_labels={*edge_label_names, *(known.edge_labels if known else ())},
            vertex_labels={*body.vertex_labels, *(known.vertex_labels if known else ())},
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc),
        ) from exc

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
        search_indexes={k: v.model_dump() for k, v in body.search_indexes.items()},
        edge_endpoints=edge_endpoints,
        strict_endpoints=body.strict_endpoints,
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
        # Declared pairs are replaced only when the request states them: a
        # client that knows nothing of them must not wipe them by accident.
        await persist_spec_replace(
            pool, spec, replace_endpoints="edge_endpoints" in body.model_fields_set,
        )
    else:
        await persist_spec(pool, spec)
    # Persisting merges with the stored spec (unless force_shrink); keep the
    # merged result in memory so a partial re-registration never hides labels
    # or resolve rules registered earlier.
    spec = await load_spec(pool, spec.name) or spec
    remember(spec)
    # A search index over every label ("*") also covers the labels this
    # registration added; build what the merged spec now needs.
    if spec.search_indexes:
        await ensure_search_indexes(pool, spec)

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
        n_edge_endpoints=len(spec.edge_endpoints),
        strict_endpoints=bool(spec.strict_endpoints),
    )


@router.delete(
    "/{name}/edge_endpoints",
    summary="Remove one declared label pair of an edge type",
    description=(
        "Removes `src -[type]-> dst` from the tenant's declared endpoints. "
        "No data is touched. When the type has no declared pairs left, it is "
        "open again (any label pair accepted, even with `strict_endpoints`)."
    ),
    responses={404: {"description": "Tenant not registered, or the pair is not declared."}},
)
async def delete_edge_endpoint(
    name: str,
    type: str,  # noqa: A002 - the API's name for an edge type
    src: str,
    dst: str,
    request: Request,
    principal: WritePrincipal,
) -> dict[str, object]:
    require_admin(principal)
    if name not in registered_tenants():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tenant '{name}' is not registered",
        )
    pool = request.app.state.pool
    if not await remove_edge_endpoint(pool, name, type, src, dst):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{src}-[{type}]->{dst} is not a declared pair of '{name}'",
        )
    spec = await load_spec(pool, name)
    if spec is not None:
        remember(spec)
    return {"tenant": name, "type": type, "src": src, "dst": dst, "status": "removed"}


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
    principal: WritePrincipal,
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

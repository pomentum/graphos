"""Tenant management endpoints.

Surface:

- ``GET  /v1/tenants``           list currently-registered tenants
  (``?unregistered=true``: also those unregistered with their data kept)
- ``POST /v1/tenants``           register or re-apply a tenant spec (idempotent)
- ``GET  /v1/tenants/{name}``    inspect one tenant's registered spec
- ``DELETE /v1/tenants/{name}``  unregister it, keeping its data
  (``?drop_data=true``: delete it with its data)
- ``POST /v1/tenants/{name}/restore``  register an unregistered tenant again
- ``DELETE /v1/tenants/{name}/edge_endpoints``  remove one declared label pair

Who may do what (grants, see ``graphos.identity``):

- register a new tenant: ``create_tenants`` on a pattern its name matches;
  the creator becomes the tenant's ``owner``;
- re-register (publish schema), remove a declared pair, inspect the spec:
  ``maintain`` on the tenant, or ``create_tenants`` on a matching pattern;
- unregister, restore, delete with its data: ``owner`` (grants on its name
  stay while it is unregistered); an orphan graph (unregistered before
  graphos kept registrations): admin only;
- the list shows the tenants the caller may maintain (admins: all).

Consumers own their schemas. graphos accepts whatever spec a token-authorised
consumer registers and brings the AGE graph up to it. Registrations are
persisted in ``graphos_meta.tenants`` and reloaded on restart; re-registering
is idempotent and cheap. See ``bootstrap.py`` module docstring for context.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response

from graphos import declared, lifecycle, schema_changes, schema_document, sync
from graphos.api.cache import LRU
from graphos.auth import (
    MaintainPrincipal,
    OwnerPrincipal,
    ReadPrincipal,
    WritePrincipal,
    require_admin,
    require_level,
)
from graphos.bootstrap import (
    TenantSpec,
    _validate_label_hierarchy,
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
    GeneratedColumnSpec,
    PropertyIndexSpec,
    RegisterDryRunResponse,
    RegisterResponse,
    TenantInfo,
    TenantList,
    TenantSpecBody,
    UnregisteredTenant,
)
from graphos.identity import Level, Principal
from graphos.resolver.indexer import ensure_resolve_rule_indexes

log = logging.getLogger(__name__)

router = APIRouter(tags=["tenants"], prefix="/tenants")


async def _record_schema(pool: asyncpg.Pool, tenant: str, detail: dict[str, Any]) -> None:
    """Best-effort: the schema change itself is already stored."""
    try:
        await schema_changes.record(pool, tenant, detail)
    except Exception:  # noqa: BLE001 — never fail the registration over its record
        log.exception("recording a schema change failed", extra={"tenant": tenant})


def _spec_to_info(spec: TenantSpec) -> TenantInfo:
    return TenantInfo(
        name=spec.name,
        vertex_labels=list(spec.vertex_labels),
        edge_labels=list(spec.edge_labels),
        property_indexes=[PropertyIndexSpec.model_validate(x) for x in spec.property_indexes],
        generated_columns=[GeneratedColumnSpec.model_validate(x) for x in spec.generated_columns],
        n_resolve_rules=len(spec.resolve_rules),
        label_hierarchy={c: p for (c, p) in spec.label_hierarchy},
        meta=dict(spec.meta),
        search_indexes=dict(spec.search_indexes),
    )


def _validate_declared(
    body: TenantSpecBody, known: TenantSpec | None, vertex_labels: set[str],
) -> None:
    """Property indexes and generated columns name vertex labels, and a
    column keeps the definition it was registered with."""
    for idx in body.property_indexes:
        if idx.label not in vertex_labels:
            raise ValueError(f"property index on {idx.label!r}: not a vertex label of the tenant")
    stored = {
        (str(c["label"]), str(c["column"])): c
        for c in (known.generated_columns if known else ())
    }
    for col in body.generated_columns:
        if col.label not in vertex_labels:
            raise ValueError(
                f"generated column {col.column!r} on {col.label!r}: "
                "not a vertex label of the tenant"
            )
        before = stored.get((col.label, col.column))
        if before is not None and before != col.model_dump():
            raise ValueError(
                f"generated column {col.label}.{col.column} is registered as "
                f"{declared.describe_column(before)}; a column cannot change"
            )


async def _dry_run(
    pool: asyncpg.Pool, previous: TenantSpec | None, spec: TenantSpec,
    *, replace: bool, endpoints_stated: bool,
) -> RegisterDryRunResponse:
    """What registering ``spec`` would change, checked as bootstrap would."""
    after = schema_document.merged(
        previous, spec, replace=replace, endpoints_stated=endpoints_stated,
    )
    try:
        _validate_label_hierarchy(spec.label_hierarchy, spec.vertex_labels)
        async with pool.acquire() as conn:
            for name, body in spec.search_indexes.items():
                for label in body.get("labels") or ():
                    if label != "*" and label not in after.vertex_labels:
                        raise ValueError(f"search index {name!r}: {label!r} is not a vertex label")
                if body.get("analyzer") == "text" and not await conn.fetchval(
                    "SELECT 1 FROM pg_ts_config WHERE cfgname = $1;", body.get("language"),
                ):
                    raise ValueError(
                        f"search index {name!r}: unknown text-search language "
                        f"{body.get('language')!r}"
                    )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc),
        ) from exc
    return RegisterDryRunResponse(
        tenant=spec.name,
        exists=previous is not None,
        mode="replace" if replace else "merge",
        changes=schema_changes.diff(previous, after),
        warnings=await schema_document.warnings(pool, previous, after, replace=replace),
    )


def _may_maintain(principal: Principal, name: str) -> bool:
    return principal.has(name, Level.MAINTAIN) or principal.can_create(name)


@router.get(
    "", summary="List the registered tenants the caller may maintain",
    response_model=TenantList, response_model_exclude_none=True,
)
async def list_tenants(
    principal: ReadPrincipal, request: Request, unregistered: bool = False,
) -> TenantList:
    out = TenantList(
        tenants=[
            _spec_to_info(s) for name, s in registered_tenants().items()
            if _may_maintain(principal, name)
        ],
    )
    if unregistered:
        found = await lifecycle.list_unregistered(request.app.state.pool)
        out.unregistered = [
            UnregisteredTenant(**asdict(u)) for u in found
            if principal.admin
            or (u.kind == "unregistered" and principal.has(u.name, Level.OWNER))
        ]
    return out


@router.get("/{name}", summary="Inspect one registered tenant")
async def get_tenant(name: str, principal: MaintainPrincipal) -> TenantInfo:
    if not _may_maintain(principal, name):
        require_level(principal, name, Level.MAINTAIN)
    specs = registered_tenants()
    if name not in specs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tenant '{name}' is not registered",
        )
    return _spec_to_info(specs[name])


@router.get(
    "/{name}/spec",
    summary="Export a tenant's schema",
    description=(
        "The whole schema as a file (`graphos.schema`, version 1): a registration body, so "
        "posting it to `POST /v1/tenants` registers that schema (rename `name` to copy it), "
        "plus an `_export` header that registration checks and ignores. Lossless (resolve "
        "rules, declared pairs and `strict_endpoints` included) and deterministic: two "
        "exports of the same schema differ only in `_export.exported_at`. No data, grants or "
        "computed values. `maintain`, as `GET /v1/tenants/{name}`."
    ),
    responses={200: {"content": {"application/json": {}}}, 404: {"description": "Not registered."}},
)
async def export_spec(name: str, request: Request, principal: MaintainPrincipal) -> Response:
    if not _may_maintain(principal, name):
        require_level(principal, name, Level.MAINTAIN)
    spec = await load_spec(request.app.state.pool, name)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"tenant '{name}' is not registered",
        )
    doc = schema_document.document(spec, now=datetime.now(UTC))
    return Response(
        content=json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}.schema.json"'},
    )


@router.post(
    "", summary="Register a tenant (idempotent)",
    response_model=RegisterResponse | RegisterDryRunResponse,
)
async def register(
    body: TenantSpecBody,
    request: Request,
    principal: WritePrincipal,
    force_shrink: bool = False,
    dry_run: bool = False,
) -> RegisterResponse | RegisterDryRunResponse:
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

    A new tenant needs ``create_tenants`` on a matching pattern and makes the
    caller its owner; an existing one needs ``maintain`` on it (or
    ``create_tenants`` on a matching pattern).

    ``?dry_run=true`` checks the request as a registration would and answers
    what it would change (the change feed's record of it) and what applying
    it would cost, writing nothing.
    """
    if not principal.can_create(body.name) and not principal.has(body.name, Level.MAINTAIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"registering tenant '{body.name}' needs 'create_tenants' on a pattern it "
                f"matches, or 'maintain' on it"
            ),
        )

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
    # Declared pairs, indexes and columns may name labels and types registered
    # earlier, or added now.
    known = registered_tenants().get(body.name)
    try:
        vertex_labels = {*body.vertex_labels, *(known.vertex_labels if known else ())}
        validate_edge_endpoints(
            edge_endpoints,
            edge_labels={*edge_label_names, *(known.edge_labels if known else ())},
            vertex_labels=vertex_labels,
        )
        _validate_declared(body, known, vertex_labels)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc),
        ) from exc

    spec = TenantSpec(
        name=body.name,
        vertex_labels=tuple(body.vertex_labels),
        edge_labels=edge_label_names_t,
        property_indexes=tuple(x.model_dump() for x in body.property_indexes),
        generated_columns=tuple(x.model_dump() for x in body.generated_columns),
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
    previous = await load_spec(pool, spec.name)
    # A name whose data is kept unregistered is not free: registering it
    # restores it, for whoever may (never hands old data to a new creator).
    restores = previous is None and await _restores(pool, spec.name, principal)
    if previous is None and not restores and not principal.can_create(spec.name):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"tenant '{spec.name}' does not exist; creating it needs 'create_tenants'",
        )
    if dry_run:
        known = await lifecycle.unregistered_spec(pool, spec.name) if restores else previous
        result = await _dry_run(
            pool, known, spec, replace=force_shrink,
            endpoints_stated="edge_endpoints" in body.model_fields_set,
        )
        return result.model_copy(update={"exists": previous is not None, "restores": restores})
    if restores:
        await lifecycle.restore_tenant(pool, spec.name)
        await _record_schema(pool, spec.name, {"restored": True})
        previous = await load_spec(pool, spec.name)
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
    change = schema_changes.diff(previous, spec)
    if change is not None:
        await _record_schema(pool, spec.name, change)
    # The creator owns what it creates (as in PostgreSQL); an admin needs no grant.
    store = getattr(request.app.state, "access_store", None)
    if previous is None and store is not None and principal.subject and not principal.admin:
        await store.grant(principal.subject, spec.name, Level.OWNER.label, by="creator")
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
        n_property_indexes=len(spec.property_indexes),
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
    principal: MaintainPrincipal,
) -> dict[str, object]:
    if not _may_maintain(principal, name):
        require_level(principal, name, Level.MAINTAIN)
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
    await _record_schema(pool, name, {"removed": {"edge_endpoints": [[type, src, dst]]}})
    return {"tenant": name, "type": type, "src": src, "dst": dst, "status": "removed"}


@router.delete(
    "/{name}",
    summary="Unregister a tenant, keeping its data; or delete it with its data",
    description=(
        "Without `drop_data`: the tenant is unregistered and its data kept. Its "
        "registration (schema, declared endpoints, settings) and the grants on its name "
        "are kept too, so `POST /v1/tenants/{name}/restore` brings it back as it was; "
        "data-plane calls answer 404 meanwhile. With `drop_data=true`: the tenant, "
        "registered or unregistered, is deleted for good: its AGE graph and data, its "
        "registration, cache, community map, finished jobs, change records and the grants "
        "on its name. That cannot be undone (download the dataset first). An orphan graph "
        "(unregistered before 0.8.2, no kept registration) is deleted only by an admin."
    ),
    responses={
        404: {"description": "No such tenant (registered, or with `drop_data`, unregistered)."},
        409: {"description": "`drop_data`: a job runs on the tenant, or its tables stay in use."},
    },
)
async def unregister(
    name: str,
    request: Request,
    principal: OwnerPrincipal,
    drop_data: bool = False,
) -> dict[str, object]:
    if not drop_data:
        require_level(principal, name, Level.OWNER)
        pool = request.app.state.pool
        if not await unregister_tenant(pool, name, by=principal.consumer):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"tenant '{name}' is not registered",
            )
        await _record_schema(pool, name, {"unregistered": True})
        return {"tenant": name, "status": "unregistered"}

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        state = await lifecycle.tenant_state(conn, name)
    if state == "orphan":
        require_admin(principal)
    else:
        require_level(principal, name, Level.OWNER)
    try:
        dropped = state is not None and await lifecycle.drop_tenant_data(pool, name)
    except lifecycle.TenantBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not dropped:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no tenant named '{name}'",
        )
    store = getattr(request.app.state, "access_store", None)
    if store is not None:
        await store.drop_tenant(name)
    LRU.evict_tenant(name)
    async with pool.acquire() as conn:
        await sync.notify_cache(conn, name, None)
    await _record_schema(pool, name, {"data_deleted": True})
    return {"tenant": name, "status": "deleted"}


@router.post(
    "/{name}/restore",
    summary="Register an unregistered tenant again, as it was",
    description=(
        "Brings back a tenant unregistered with its data kept: its schema, declared "
        "endpoints and settings as they were when it was unregistered. Needs `owner` on it "
        "(grants on its name stay while it is unregistered)."
    ),
    responses={
        404: {"description": "No unregistered tenant by that name."},
        409: {"description": "It is registered, or an orphan graph with no kept registration."},
    },
)
async def restore(name: str, request: Request, principal: OwnerPrincipal) -> dict[str, object]:
    require_level(principal, name, Level.OWNER)
    pool = request.app.state.pool
    if not await lifecycle.restore_tenant(pool, name):
        async with pool.acquire() as conn:
            state = await lifecycle.tenant_state(conn, name)
        if state == "registered":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"tenant '{name}' is registered",
            )
        if state == "orphan":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"the graph '{name}' has no kept registration (it was unregistered before "
                    "graphos 0.8.2): an admin registers it again with its schema"
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no unregistered tenant named '{name}'",
        )
    await _record_schema(pool, name, {"restored": True})
    return {"tenant": name, "status": "restored"}


async def _restores(pool: asyncpg.Pool, name: str, principal: Principal) -> bool:
    """Whether registering the unregistered ``name`` restores it; raises 409
    when its kept data is not the caller's to take."""
    async with pool.acquire() as conn:
        state = await lifecycle.tenant_state(conn, name)
    if state == "unregistered":
        if principal.has(name, Level.OWNER):
            return True
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"tenant '{name}' is unregistered and keeps its data: its owner or an admin "
                "can restore it, or delete it with its data, before the name is free"
            ),
        )
    if state == "orphan" and not principal.admin:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"a graph named '{name}' holds data without a registration (unregistered "
                "before graphos 0.8.2): only an admin can register it again or delete it"
            ),
        )
    return False

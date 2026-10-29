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
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import asyncpg
from fastapi import APIRouter, Header, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse, Response

from graphos import (
    by_entry,
    changes,
    declared,
    lifecycle,
    renames,
    schema_breaking,
    schema_changes,
    schema_document,
    schema_drift,
    schema_rename,
    schema_version,
    sync,
)
from graphos.api.cache import LRU
from graphos.auth import (
    MaintainPrincipal,
    OwnerPrincipal,
    ReadPrincipal,
    require_admin,
    require_level,
)
from graphos.bootstrap import (
    DEFAULT_COMMUNITY_ALGORITHM,
    TenantSpec,
    _validate_label_hierarchy,
    bootstrap_tenant,
    ensure_search_indexes,
    forget_removals,
    load_removals,
    load_spec,
    load_spec_versioned,
    persist_spec,
    persist_spec_replace,
    record_removal,
    registered_tenants,
    remember,
    remove_edge_endpoint,
    set_schema_manager,
    unregister_tenant,
    validate_display,
    validate_edge_endpoints,
)
from graphos.contracts.property_schema import PropertySchemaSpec
from graphos.contracts.tenants import (
    BreakingChange,
    DeprecatedSpec,
    DisplaySpec,
    DriftReport,
    GeneratedColumnSpec,
    PropertyIndexSpec,
    RegisterDryRunResponse,
    RegisterResponse,
    SchemaDropResponse,
    SchemaRename,
    SchemaRenameRequest,
    SchemaRenameResponse,
    TenantInfo,
    TenantList,
    TenantSpecBody,
    UnregisteredTenant,
)
from graphos.identity import Level, Principal
from graphos.lookup import IfMatch, PreconditionFailedError
from graphos.properties import SchemaError, definitions_count, validate_schema
from graphos.resolver.indexer import ensure_resolve_rule_indexes

log = logging.getLogger(__name__)

router = APIRouter(tags=["tenants"], prefix="/tenants")

TenantName = Annotated[str, Path(description="The tenant's name.")]

# The schema document: a registration body plus its ``_export`` header.
_SCHEMA_DOCUMENT = {
    "allOf": [{"$ref": "#/components/schemas/TenantSpecBody"}],
    "type": "object",
    "properties": {
        "_export": {
            "type": "object",
            "description": "What the file is; a registration checks it and ignores it.",
            "properties": {
                "format": {"type": "string", "enum": [schema_document.FORMAT]},
                "version": {"type": "integer", "enum": [1, 2, 3, 4]},
                "graphos": {"type": "string", "description": "The exporting graphos version."},
                "tenant": {"type": "string"},
                "exported_at": {"type": "string", "format": "date-time"},
            },
        },
    },
    "required": ["_export", "name"],
}


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
        display=DisplaySpec.model_validate(spec.display),
        property_schema=PropertySchemaSpec.model_validate(spec.property_schema),
        deprecated=DeprecatedSpec.model_validate(spec.deprecated),
        renames=[SchemaRename.model_validate({"status": "done", **r}) for r in spec.renames],
        community_algorithm=spec.community_algorithm or DEFAULT_COMMUNITY_ALGORITHM,
        schema_manager=spec.schema_manager,
        schema_manager_since=spec.schema_manager_since,
    )


def _property_schema(body: TenantSpecBody) -> tuple[dict[str, Any], by_entry.Removed]:
    """The property schema a registration declares and the entries it removes.
    Definitions keep only the keys stated, so a narrowing entry inherits the rest."""
    request = body.property_schema
    entries, removed = by_entry.split(
        request, lambda e: e.model_dump(exclude_none=True, exclude_defaults=True),
    )
    mode = {"mode": request.mode} if request.mode else {}
    return {**mode, **entries}, removed


def _validate_property_schema(
    previous: TenantSpec | None, incoming: dict[str, Any], removed: by_entry.Removed,
    *, vertex_labels: set[str], edge_labels: set[str], hierarchy: dict[str, str], replace: bool,
) -> None:
    """The schema the registration would leave names the tenant's labels and compiles."""
    after = incoming if replace or previous is None else schema_document.merged_property_schema(
        previous.property_schema, incoming, removed,
    )
    try:
        validate_schema(after, vertex_labels, edge_labels, hierarchy)
    except SchemaError as exc:
        raise ValueError(f"property_schema: {exc}") from exc


def _display(body: TenantSpecBody) -> tuple[dict[str, Any], by_entry.Removed]:
    """The display entries a registration declares, and those it removes."""
    return by_entry.split(body.display, lambda e: e.model_dump(exclude_none=True))


def _deprecated(body: TenantSpecBody) -> tuple[dict[str, Any], by_entry.Removed]:
    """The deprecations a registration declares, and those it removes."""
    return by_entry.split(
        body.deprecated, lambda e: e.model_dump(exclude_none=True, exclude_defaults=True),
    )


def _validate_deprecated(
    deprecated: dict[str, Any], *, vertex_labels: set[str], edge_labels: set[str],
) -> None:
    """Deprecations name the tenant's labels and types, and what replaces a label
    or type is one of them too."""
    for part, known, kind in (
        ("vertex", vertex_labels, "vertex label"), ("edge", edge_labels, "edge label"),
    ):
        for name, entry in (deprecated.get(part) or {}).items():
            if name not in known:
                raise ValueError(f"deprecated: {name!r} is not a {kind} of the tenant")
            replacement = entry.get("replaced_by")
            if replacement is not None and replacement not in known:
                raise ValueError(
                    f"deprecated: {name!r} is replaced by {replacement!r}, "
                    f"not a {kind} of the tenant"
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
    *, replace: bool, endpoints_stated: bool, meta_removed: tuple[str, ...] = (),
    display_removed: tuple[tuple[str, str], ...] = (),
    schema_removed: tuple[tuple[str, str], ...] = (),
    deprecated_removed: tuple[tuple[str, str], ...] = (),
    declared: TenantSpec | None = None,
) -> RegisterDryRunResponse:
    """What registering ``spec`` would change, checked as bootstrap would.

    ``declared`` is the registration as sent, before its styles merge
    (``schema_drift.apply_display_merge``): the drift report is about it.
    """
    after = schema_document.merged(
        previous, spec, replace=replace, endpoints_stated=endpoints_stated,
        meta_removed=meta_removed, display_removed=display_removed,
        schema_removed=schema_removed, deprecated_removed=deprecated_removed,
    )
    try:
        _validate_label_hierarchy(spec.label_hierarchy, spec.vertex_labels)
        validate_display(spec.display, set(after.edge_labels), set(after.vertex_labels))
        async with pool.acquire() as conn:
            for name, body in spec.search_indexes.items():
                for label in cast("Iterable[str]", body.get("labels") or ()):
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
    changes = schema_changes.diff(previous, after)
    return RegisterDryRunResponse(
        tenant=spec.name,
        exists=previous is not None,
        mode="replace" if replace else "merge",
        changes=changes,
        warnings=await schema_document.warnings(pool, previous, after, replace=replace),
        breaking=await _breaking(pool, previous, after, replace=replace),
        drift=None if previous is None else DriftReport(**schema_drift.drift(
            previous, declared or spec, after, replace_all=replace,
            removals=await load_removals(pool, spec.name),
        )),
    )


def _judge(
    stored: TenantSpec | None, spec: TenantSpec, principal: Principal, body: TenantSpecBody, *,
    replace: bool, endpoints_stated: bool, meta_removed: tuple[str, ...],
    display_removed: by_entry.Removed, schema_removed: by_entry.Removed,
    deprecated_removed: by_entry.Removed, claims: bool = False,
) -> tuple[TenantSpec, by_entry.Removed]:
    """A registration's styles merged as it says (the manager's default to
    ``missing_only``, and that of a registration naming its caller the
    manager, ``claims``), and on a managed tenant, anyone else's held to
    styles only (409 otherwise; ADR-0013, ADR-0014)."""
    manager = stored.schema_manager if stored else None
    is_manager = manager is not None and schema_drift.principal_subject(principal) == manager
    mode = body.display_merge or ("missing_only" if is_manager or claims else "entries")
    spec, display_removed = schema_drift.apply_display_merge(
        stored, spec, display_removed, mode, replace_all=replace,
    )
    if stored is None or manager is None or is_manager:
        return spec, display_removed
    after = schema_document.merged(
        stored, spec, replace=replace, endpoints_stated=endpoints_stated,
        meta_removed=meta_removed, display_removed=display_removed,
        schema_removed=schema_removed, deprecated_removed=deprecated_removed,
    )
    if schema_drift.changes_structure(schema_changes.diff(stored, after)):
        raise schema_drift.SchemaManaged(spec.name, manager)
    return spec, display_removed


def _claims(manager_stated: bool, manager: str | None, principal: Principal) -> bool:
    """Whether a registration names its own caller the schema manager: its
    styles then merge as the manager's do, from that first registration on."""
    return (
        manager_stated and manager is not None
        and manager == schema_drift.principal_subject(principal)
    )


async def _breaking(
    pool: asyncpg.Pool, previous: TenantSpec | None, after: TenantSpec, *, replace: bool,
) -> list[BreakingChange]:
    """The dry run's breaking changes, each with its label's or type's row estimate."""
    found = schema_breaking.changes(previous, after, replace=replace)
    if not found:
        return []
    rows: dict[str, int | None] = {}
    async with pool.acquire() as conn:
        for name in sorted({b.name for b in found if b.name is not None}):
            rows[name] = await schema_document.row_estimate(conn, after.name, name)
    return [
        BreakingChange(
            part=b.part, name=b.name, change=b.change, detail=b.detail,
            rows=rows.get(b.name) if b.name is not None else None,
        )
        for b in found
    ]


def _may_maintain(principal: Principal, name: str) -> bool:
    return principal.has(name, Level.MAINTAIN) or principal.can_create(name)


# ── Schema versions: ETag / If-Match (graphos.schema_version) ───────────────

SchemaIfMatch = Annotated[
    str | None,
    Header(
        description=(
            "The schema's ETag from an earlier read (`GET /v1/{tenant}/schema`, "
            "`GET /v1/tenants/{name}`, the exported document), or `*`: the write happens only "
            "if the schema has not changed since (412 otherwise, with its current ETag). "
            "Optional."
        ),
    ),
]

# The 409 of a schema write on a tenant someone else manages (ADR-0013).
_SCHEMA_MANAGED = (
    " Or the tenant's schema manager is someone else and the write would change its structure: "
    "`{\"detail\", \"code\": \"schema_managed\", \"schema_manager\"}`, nothing written."
)

_SCHEMA_PRECONDITION: dict[int | str, dict[str, Any]] = {
    412: {"description": (
        "`If-Match` did not match: the schema changed since it was read (its current `ETag` "
        "is in the answer); nothing was written."
    )},
}


def _if_match(header: str | None) -> IfMatch | None:
    return None if header is None else IfMatch.parse(header)


def _schema_changed(name: str, exc: PreconditionFailedError) -> HTTPException:
    if exc.current is None:
        return HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail=f"If-Match failed: tenant '{name}' is not registered",
        )
    return HTTPException(
        status_code=status.HTTP_412_PRECONDITION_FAILED,
        detail=(
            f"If-Match failed: the schema of '{name}' changed since it was read; "
            f"its ETag is now {exc.current}"
        ),
        headers={"ETag": exc.current},
    )


async def _versioned(request: Request, name: str) -> tuple[TenantSpec, str]:
    """The stored spec and its ETag, read together; 404 when not registered."""
    found = await load_spec_versioned(request.app.state.pool, name)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"tenant '{name}' is not registered",
        )
    return found[0], schema_version.etag(found[1])


@router.get(
    "", summary="List the registered tenants the caller may maintain",
    description=(
        "Any token. The registered tenants the caller may maintain (`maintain` on it, or "
        "`create_tenants` on a pattern its name matches; every tenant for an admin), each "
        "with its registered spec as `GET /v1/tenants/{name}` answers it. With "
        "`unregistered=true`, also the tenants unregistered with their data kept that the "
        "caller owns; an admin also sees orphan graphs."
    ),
    response_model=TenantList, response_model_exclude_none=True,
)
async def list_tenants(
    principal: ReadPrincipal,
    request: Request,
    unregistered: Annotated[
        bool, Query(description="Also list the tenants unregistered with their data kept."),
    ] = False,
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


@router.get(
    "/{name}",
    summary="Inspect one registered tenant",
    description=(
        "`maintain` on the tenant, or `create_tenants` on a pattern its name matches. Its "
        "registered spec: labels, property indexes, generated columns, the number of resolve "
        "rules, label hierarchy, metadata, search indexes, display hints and property schema. "
        "The whole schema as a file is `GET /v1/tenants/{name}/spec`. The `ETag` is the "
        "schema's version, for `If-Match` on schema writes."
    ),
    responses={404: {"description": "Not registered."}},
)
async def get_tenant(
    name: TenantName, request: Request, response: Response, principal: MaintainPrincipal,
) -> TenantInfo:
    if not _may_maintain(principal, name):
        require_level(principal, name, Level.MAINTAIN)
    spec, tag = await _versioned(request, name)
    response.headers["ETag"] = tag
    return _spec_to_info(spec)


@router.get(
    "/{name}/spec",
    summary="Export a tenant's schema",
    description=(
        "The whole schema as a file (`graphos.schema`, version 1, or 2 when it declares a "
        "property schema): a registration body, so "
        "posting it to `POST /v1/tenants` registers that schema (rename `name` to copy it), "
        "plus an `_export` header that registration checks and ignores. Lossless (resolve "
        "rules, declared pairs and `strict_endpoints` included) and deterministic: two "
        "exports of the same schema differ only in `_export.exported_at`. No data, grants or "
        "computed values. `maintain`, as `GET /v1/tenants/{name}`."
    ),
    responses={
        200: {
            "description": (
                "The schema document, sent as the attachment `<name>.schema.json`."
            ),
            "content": {"application/json": {"schema": _SCHEMA_DOCUMENT}},
        },
        404: {"description": "Not registered."},
    },
)
async def export_spec(name: TenantName, request: Request, principal: MaintainPrincipal) -> Response:
    if not _may_maintain(principal, name):
        require_level(principal, name, Level.MAINTAIN)
    spec, tag = await _versioned(request, name)
    doc = schema_document.document(spec, now=datetime.now(UTC))
    return Response(
        content=json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}.schema.json"', "ETag": tag},
    )


@router.post(
    "", summary="Register a tenant (idempotent)",
    response_model=RegisterResponse | RegisterDryRunResponse,
    responses={409: {"description": (
        "The name is an unregistered tenant that keeps its data, which only its owner or an "
        "admin may restore; or an orphan graph, which only an admin may register again."
        + _SCHEMA_MANAGED
    )}, **_SCHEMA_PRECONDITION},
)
async def register(
    body: TenantSpecBody,
    request: Request,
    response: Response,
    principal: MaintainPrincipal,
    if_match: SchemaIfMatch = None,
    force_shrink: Annotated[
        bool,
        Query(description=(
            "Replace the stored schema with the one sent instead of merging into it. "
            "Labels left out are not dropped from the graph."
        )),
    ] = False,
    dry_run: Annotated[
        bool, Query(description="Answer what the registration would change, writing nothing."),
    ] = False,
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
    display, display_removed = _display(body)
    property_schema, schema_removed = _property_schema(body)
    deprecated, deprecated_removed = _deprecated(body)
    if known is not None:
        # A name renamed away is not created again by an old client registering it.
        for name in (*body.vertex_labels, *edge_label_names):
            renames.refuse_renamed(known, name)
    try:
        vertex_labels = {*body.vertex_labels, *(known.vertex_labels if known else ())}
        edge_labels = {*edge_label_names, *(known.edge_labels if known else ())}
        validate_edge_endpoints(
            edge_endpoints, edge_labels=edge_labels, vertex_labels=vertex_labels,
        )
        _validate_declared(body, known, vertex_labels)
        # Replacing the schema keeps only the labels sent: entries must name those.
        if force_shrink:
            validate_display(display, set(edge_label_names), set(body.vertex_labels))
        else:
            validate_display(display, edge_labels, vertex_labels)
        _validate_property_schema(
            known, property_schema, schema_removed,
            vertex_labels=set(body.vertex_labels) if force_shrink else vertex_labels,
            edge_labels=set(edge_label_names) if force_shrink else edge_labels,
            hierarchy=(
                dict(body.label_hierarchy) if force_shrink
                else {**dict(known.label_hierarchy if known else ()), **body.label_hierarchy}
            ),
            replace=force_shrink,
        )
        _validate_deprecated(
            deprecated,
            vertex_labels=set(body.vertex_labels) if force_shrink else vertex_labels,
            edge_labels=set(edge_label_names) if force_shrink else edge_labels,
        )
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
        meta={k: v for k, v in body.meta.items() if v is not None},
        search_indexes={k: v.model_dump() for k, v in body.search_indexes.items()},
        display=display,
        property_schema=property_schema,
        deprecated=deprecated,
        edge_endpoints=edge_endpoints,
        strict_endpoints=body.strict_endpoints,
        community_algorithm=body.community_algorithm,
    )

    # ``null`` removes a key from the stored meta (a merge); replacing keeps only those sent.
    meta_removed = tuple(sorted(k for k, v in body.meta.items() if v is None))
    manager_stated, manager = _wanted_manager(request, body, principal)
    pool = request.app.state.pool
    wanted = _if_match(if_match)
    if dry_run:
        previous, restores, judged_tag = await _existing(pool, spec.name, principal)
        try:
            await schema_version.check(pool, spec.name, wanted)
        except PreconditionFailedError as exc:
            raise _schema_changed(spec.name, exc) from exc
        known = await lifecycle.unregistered_spec(pool, spec.name) if restores else previous
        endpoints_stated = "edge_endpoints" in body.model_fields_set
        manager_change = _manager_change(known, manager_stated, manager, principal, spec.name)
        judged, display_removed = _judge(
            known, spec, principal, body, replace=force_shrink,
            endpoints_stated=endpoints_stated, meta_removed=meta_removed,
            display_removed=display_removed, schema_removed=schema_removed,
            deprecated_removed=deprecated_removed,
            claims=_claims(manager_stated, manager, principal),
        )
        result = await _dry_run(
            pool, known, judged, declared=spec, replace=force_shrink,
            endpoints_stated=endpoints_stated, meta_removed=meta_removed,
            display_removed=display_removed, schema_removed=schema_removed,
            deprecated_removed=deprecated_removed,
        )
        if manager_change is not None:
            result.changes = {**(result.changes or {}), "schema_manager": manager_change}
        # The version of the spec the preview judged (read with it): sent back
        # as If-Match, the registration applies exactly this (ADR-0014).
        if judged_tag is not None:
            response.headers["ETag"] = judged_tag
        return result.model_copy(update={"exists": previous is not None, "restores": restores})
    try:
        async with schema_version.write(pool, spec.name, wanted) as written:
            answer = await _register(
                request, principal, body, spec, force_shrink=force_shrink,
                meta_removed=meta_removed, display_removed=display_removed,
                schema_removed=schema_removed, deprecated_removed=deprecated_removed,
                manager_stated=manager_stated, manager=manager,
            )
            tag = await written.etag()
    except PreconditionFailedError as exc:
        raise _schema_changed(spec.name, exc) from exc
    if tag is not None:
        response.headers["ETag"] = tag
    return answer


async def _refuse_if_managed(pool: asyncpg.Pool, name: str, principal: Principal) -> None:
    """409 when the tenant has a schema manager and the caller is not it (ADR-0013):
    for the writes that change structure only (renames, drops, pair removals)."""
    stored = await load_spec(pool, name)
    schema_drift.check(name, stored.schema_manager if stored else None, principal)


def _wanted_manager(
    request: Request, body: TenantSpecBody, principal: Principal,
) -> tuple[bool, str | None]:
    """Whether the registration names a schema manager, and which (``self``
    resolved to the caller); 422 for one that cannot be (ADR-0014)."""
    if "schema_manager" not in body.model_fields_set:
        return False, None
    value = body.schema_manager
    if value is None:
        return True, None
    if request.app.state.settings.auth_disabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="a schema manager needs sign-in; authentication is disabled here",
        )
    if value == "self":
        value = schema_drift.principal_subject(principal)
        if value is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="\"self\" names the caller, and this caller has no name to manage with",
            )
    store = getattr(request.app.state, "access_store", None)
    if value.startswith("user:") and store is not None:
        user = store.user(value.removeprefix("user:"))
        if user is None or user.disabled:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"schema manager {value}: no such enabled user or service account",
            )
    return True, value


def _manager_change(
    stored: TenantSpec | None, stated: bool, manager: str | None, principal: Principal,
    name: str,
) -> list[str | None] | None:
    """``[before, after]`` when the registration changes the schema manager, else
    ``None``; 403 unless the caller owns the tenant (the creator of a new one
    does). Judged on the stored tenant, under the lock for a registration."""
    current = stored.schema_manager if stored else None
    if not stated or manager == current:
        return None
    if stored is not None and not (principal.admin or principal.has(name, Level.OWNER)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"changing the schema manager of '{name}' needs 'owner' on it",
        )
    return [current, manager]


async def _existing(
    pool: asyncpg.Pool, name: str, principal: Principal,
) -> tuple[TenantSpec | None, bool, str | None]:
    """The stored spec, whether registering ``name`` restores it, and the ETag
    of that spec (read with it); 403 when the caller may not create it."""
    found = await load_spec_versioned(pool, name)
    previous, tag = (None, None) if found is None else (found[0], schema_version.etag(found[1]))
    # A name whose data is kept unregistered is not free: registering it
    # restores it, for whoever may (never hands old data to a new creator).
    restores = previous is None and await _restores(pool, name, principal)
    if previous is None and not restores and not principal.can_create(name):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"tenant '{name}' does not exist; creating it needs 'create_tenants'",
        )
    return previous, restores, tag


async def _register(
    request: Request, principal: Principal, body: TenantSpecBody, spec: TenantSpec, *,
    force_shrink: bool, meta_removed: tuple[str, ...],
    display_removed: by_entry.Removed, schema_removed: by_entry.Removed,
    deprecated_removed: by_entry.Removed, manager_stated: bool, manager: str | None,
) -> RegisterResponse:
    """Apply a registration, under the tenant's schema-write lock."""
    pool = request.app.state.pool
    previous, restores, _ = await _existing(pool, spec.name, principal)
    # Judged here, under the lock, on the schema as it is now (an unregistered
    # tenant's as it would be restored): a refused registration restores nothing.
    known = await lifecycle.unregistered_spec(pool, spec.name) if restores else previous
    manager_change = _manager_change(known, manager_stated, manager, principal, spec.name)
    spec, display_removed = _judge(
        known, spec, principal, body, replace=force_shrink,
        endpoints_stated="edge_endpoints" in body.model_fields_set,
        meta_removed=meta_removed, display_removed=display_removed,
        schema_removed=schema_removed, deprecated_removed=deprecated_removed,
        claims=_claims(manager_stated, manager, principal),
    )
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
        await persist_spec(
            pool, spec, meta_removed=meta_removed, display_removed=display_removed,
            schema_removed=schema_removed, deprecated_removed=deprecated_removed,
        )
    # Persisting merges with the stored spec (unless force_shrink); keep the
    # merged result in memory so a partial re-registration never hides labels
    # or resolve rules registered earlier.
    stored = await load_spec(pool, spec.name) or spec
    change = schema_changes.diff(previous, stored) or {}
    if manager_change is not None:
        change["schema_manager"] = manager_change
    # The manager, the removal records and the change record together (ADR-0014).
    async with pool.acquire() as conn, conn.transaction():
        if manager_change is not None:
            await set_schema_manager(conn, spec.name, manager)
        if force_shrink:
            # What a replace removes is a removal too: reported if declared again.
            removed = change.get("removed") or {}
            for kind, part in schema_drift.REMOVAL_PARTS.items():
                if kind == "edge_endpoint":
                    continue
                for name in removed.get(part) or ():
                    await record_removal(conn, spec.name, kind, name, by=principal.consumer)
            for edge_type, src, dst in removed.get("edge_endpoints") or ():
                await record_removal(conn, spec.name, "edge_endpoint", edge_type, src, dst,
                                     by=principal.consumer)
        # What it declared again after a drop is no longer a removal (ADR-0013).
        await forget_removals(conn, spec.name, stored)
        if change:
            await changes.record(
                conn, tenant=spec.name, op="schema", kind="tenant",
                count=schema_changes.size(change), detail=change,
            )
    spec = await load_spec(pool, spec.name) or stored
    remember(spec)
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
        community_algorithm=spec.community_algorithm or DEFAULT_COMMUNITY_ALGORITHM,
        n_property_definitions=definitions_count(spec.property_schema),
        property_mode=spec.property_schema.get("mode", "off"),
        schema_manager=spec.schema_manager,
    )


@router.post(
    "/{name}/schema:rename",
    summary="Rename a vertex label, an edge type or a property",
    response_model=SchemaRenameResponse, response_model_by_alias=True,
    description=(
        "`owner` on the tenant (it changes what every consumer of the tenant sees).\n\n"
        "**A vertex label or edge type** is renamed in place, in one transaction: its rows, "
        "their ids and the edges between them stay as they are, and everything graphos keeps "
        "that names it (property definitions, display, declared pairs, indexes, resolve "
        "rules, label hierarchy, deprecations) is renamed with it. Afterwards a write or a "
        "read naming the old name answers 409 saying where it went, and registering it again "
        "is refused; Cypher naming it returns nothing, so the answer lists the saved queries "
        "that name it (not rewritten). One `schema` change record.\n\n"
        "**A property** (`on`: its vertex label, with the labels below it, or edge type) is "
        "renamed by a background job (202, `Location: /v1/jobs/{id}`, progress on the job): "
        "rows are rewritten in batches (each row a new revision, one `patch` record per "
        "batch); meanwhile writes naming the old name carry a warning, afterwards they are "
        "refused. Its definitions, property and search indexes and styles follow. Refused "
        "when rows have both names, or a generated column, a resolve rule or a search index "
        "over other labels uses it. A failed or cancelled rename resumes when asked again.\n\n"
        "With `dry_run=true`, what it would change, writing nothing."
    ),
    responses={
        202: {"model": SchemaRenameResponse,
              "description": "A property rename: queued as a job (`job_id`, `Location`)."},
        400: {"description": "Not a label, type or property the tenant can rename this way: "
                             "the new name is taken or invalid, the names are the same, or "
                             "something still uses the property (see the description)."},
        404: {"description": "The tenant is not registered."},
        409: {"description": "A job runs on the tenant (a property rename: another one), a "
                             "reconcile holds the edge type, or its tables stay in use (5 s)."
                             + _SCHEMA_MANAGED},
        **_SCHEMA_PRECONDITION,
    },
)
async def rename_schema_name(
    name: TenantName,
    body: SchemaRenameRequest,
    request: Request,
    response: Response,
    principal: OwnerPrincipal,
    if_match: SchemaIfMatch = None,
    dry_run: Annotated[
        bool, Query(description="Answer what the rename would change, writing nothing."),
    ] = False,
) -> SchemaRenameResponse | JSONResponse:
    require_level(principal, name, Level.OWNER)
    if registered_tenants().get(name) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"tenant '{name}' is not registered",
        )
    pool = request.app.state.pool
    wanted = _if_match(if_match)
    try:
        if dry_run:
            await schema_version.check(pool, name, wanted)
            await _refuse_if_managed(pool, name, principal)
            return await _rename(request, name, body, dry_run=True, by=principal.consumer)
        async with schema_version.write(pool, name, wanted) as written:
            await _refuse_if_managed(pool, name, principal)
            answer = await _rename(request, name, body, dry_run=False, by=principal.consumer)
            tag = await written.etag()
    except PreconditionFailedError as exc:
        raise _schema_changed(name, exc) from exc
    if tag is not None:
        # A property rename answers 202 itself; the others through ``response``.
        (answer if isinstance(answer, JSONResponse) else response).headers["ETag"] = tag
    return answer


async def _rename(
    request: Request, name: str, body: SchemaRenameRequest, *, dry_run: bool, by: str | None,
) -> SchemaRenameResponse | JSONResponse:
    """A rename, or its plan; under the tenant's schema-write lock unless a dry run."""
    # Read here, under the lock: the spec as it is now, not as it was at the request.
    spec = registered_tenants().get(name)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"tenant '{name}' is not registered",
        )
    pool = request.app.state.pool
    if body.kind == "property":
        return await _rename_property(request, name, spec, body, dry_run=dry_run, by=by)
    try:
        if dry_run:
            done = await schema_rename.plan(pool, spec, body.kind, body.from_, body.to)
        else:
            done = await schema_rename.rename(
                pool, spec, body.kind, body.from_, body.to, by=by,
                vertex_graphid_indexes=request.app.state.settings.vertex_graphid_indexes,
            )
    except schema_rename.RenameError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except lifecycle.TenantBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return SchemaRenameResponse(
        tenant=name, kind=body.kind, from_=body.from_, to=body.to, dry_run=dry_run,
        spec_parts=done.spec_parts, indexes=[list(pair) for pair in done.indexes],
        endpoints=done.endpoints, rows=done.rows, saved_queries=done.saved_queries,
    )


async def _rename_property(
    request: Request, name: str, spec: TenantSpec, body: SchemaRenameRequest,
    *, dry_run: bool, by: str | None,
) -> SchemaRenameResponse | JSONResponse:
    """A property rename: its plan, or its job queued (202)."""
    on = cast("str", body.on)
    pool = request.app.state.pool
    try:
        found = await schema_rename.plan_property(pool, spec, on, body.from_, body.to)
        if dry_run:
            return SchemaRenameResponse(
                tenant=name, kind="property", from_=body.from_, to=body.to, on=on,
                dry_run=True, spec_parts=found.spec_parts, rows=found.rows,
                saved_queries=found.saved_queries, warnings=found.warnings,
            )
        job_id = await schema_rename.start_property(pool, name, on, body.from_, body.to, by=by)
    except schema_rename.RenameError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except lifecycle.TenantBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    runner = getattr(request.app.state, "job_runner", None)
    if runner is not None:
        runner.notify()
    answer = SchemaRenameResponse(
        tenant=name, kind="property", from_=body.from_, to=body.to, on=on,
        spec_parts=found.spec_parts, rows=found.rows, saved_queries=found.saved_queries,
        job_id=job_id, warnings=found.warnings,
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=answer.model_dump(mode="json", by_alias=True),
        headers={"Location": f"/v1/jobs/{job_id}", "Retry-After": "2"},
    )


@router.delete(
    "/{name}/labels/{label}",
    summary="Drop a deprecated vertex label or edge type that has no rows",
    response_model=SchemaDropResponse,
    description=(
        "`owner` on the tenant. Drops a vertex label or edge type marked deprecated, only when "
        "it has no rows: its table and indexes, and everything graphos keeps that names it "
        "(property definitions, display, declared pairs, property indexes, generated columns, "
        "search indexes over it alone, its resolve rule), in one transaction; one `schema` "
        "change record. Refused while a label inherits from it, a resolve rule requires it as "
        "a destination, or a deprecation names it as the replacement. The answer lists the "
        "saved queries that name it (not rewritten)."
    ),
    responses={
        400: {"description": "Not a label or type of the tenant, not deprecated, it has rows, "
                             "or something still needs it (see the description)."},
        404: {"description": "The tenant is not registered."},
        409: {"description": "A job runs on the tenant, a reconcile holds the edge type, or "
                             "its tables stay in use (5 s)." + _SCHEMA_MANAGED},
        **_SCHEMA_PRECONDITION,
    },
)
async def drop_schema_label(
    name: TenantName,
    label: Annotated[str, Path(description="The vertex label or edge type.")],
    request: Request,
    response: Response,
    principal: OwnerPrincipal,
    if_match: SchemaIfMatch = None,
) -> SchemaDropResponse:
    require_level(principal, name, Level.OWNER)
    spec = registered_tenants().get(name)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"tenant '{name}' is not registered",
        )
    pool = request.app.state.pool
    try:
        async with schema_version.write(pool, name, _if_match(if_match)) as written:
            await _refuse_if_managed(pool, name, principal)
            saved = await schema_rename.drop_label(pool, name, label, by=principal.consumer)
            tag = await written.etag()
    except PreconditionFailedError as exc:
        raise _schema_changed(name, exc) from exc
    except schema_rename.RenameError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except lifecycle.TenantBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if tag is not None:
        response.headers["ETag"] = tag
    return SchemaDropResponse(
        tenant=name, kind="label" if label in spec.vertex_labels else "edge_type", name=label,
        saved_queries=saved,
    )


@router.delete(
    "/{name}/edge_endpoints",
    summary="Remove one declared label pair of an edge type",
    description=(
        "Removes `src -[type]-> dst` from the tenant's declared endpoints. "
        "No data is touched. When the type has no declared pairs left, it is "
        "open again (any label pair accepted, even with `strict_endpoints`)."
    ),
    responses={
        404: {"description": "Tenant not registered, or the pair is not declared."},
        409: {"description": _SCHEMA_MANAGED.strip()},
        **_SCHEMA_PRECONDITION,
    },
)
async def delete_edge_endpoint(
    name: TenantName,
    type: Annotated[  # noqa: A002 - the API's name for an edge type
        str, Query(description="The edge type."),
    ],
    src: Annotated[str, Query(description="The pair's source vertex label.")],
    dst: Annotated[str, Query(description="The pair's target vertex label.")],
    request: Request,
    response: Response,
    principal: MaintainPrincipal,
    if_match: SchemaIfMatch = None,
) -> dict[str, object]:
    if not _may_maintain(principal, name):
        require_level(principal, name, Level.MAINTAIN)
    if name not in registered_tenants():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tenant '{name}' is not registered",
        )
    pool = request.app.state.pool
    try:
        async with schema_version.write(pool, name, _if_match(if_match)) as written:
            await _refuse_if_managed(pool, name, principal)
            if not await remove_edge_endpoint(pool, name, type, src, dst, by=principal.consumer):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"{src}-[{type}]->{dst} is not a declared pair of '{name}'",
                )
            spec = await load_spec(pool, name)
            if spec is not None:
                remember(spec)
            await _record_schema(pool, name, {"removed": {"edge_endpoints": [[type, src, dst]]}})
            tag = await written.etag()
    except PreconditionFailedError as exc:
        raise _schema_changed(name, exc) from exc
    if tag is not None:
        response.headers["ETag"] = tag
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
    name: TenantName,
    request: Request,
    principal: OwnerPrincipal,
    drop_data: Annotated[
        bool, Query(description="Delete the tenant with its data, for good."),
    ] = False,
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
async def restore(
    name: TenantName, request: Request, principal: OwnerPrincipal,
) -> dict[str, object]:
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

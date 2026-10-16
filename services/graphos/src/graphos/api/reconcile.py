"""``POST /v1/{tenant}/graph/edges:reconcile``: make a stored edge set equal to a given one.

ADR-0001.  A consumer that reloads its source sends the whole set of edges it
wants in a scope, and graphos adds the missing ones, rewrites the kept ones
whose properties change and deletes the rest, in one transaction
(:func:`graphos.writes.reconcile_edges`).  Readers never see a half-built
graph, and a reload costs the difference, not a rebuild.

Two scopes:

- ``node``: the edges of one type at one node, in one direction, whose other
  end has a given label (a node's source record changed); a JSON body.
- ``type``: every edge of one type between two labels (a relationship
  re-extracted); a JSON body up to 10,000 edges, or NDJSON: a header line,
  then one edge per line, up to ``GRAPHOS_RECONCILE_MAX_EDGES``.

The checks are an upsert's: registered type and labels, declared pairs under
``strict_endpoints``, reserved names and the property schema.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import ValidationError

from graphos import lookup, properties, reserved, writes
from graphos.api.bulk import iter_lines
from graphos.api.graph import _check_tenant, _check_vertex_label
from graphos.auth import Tenant, WritePrincipal
from graphos.bootstrap import registered_tenants
from graphos.contracts.graph import (
    EdgesReconcileRequest,
    EdgesReconcileResponse,
    ReconcileEdge,
    ReconcileTypeHeader,
)
from graphos.cypher import CypherError
from graphos.endpoints import policy_for
from graphos.observability.metrics import record_property_tally

router = APIRouter(tags=["graph"], prefix="/{tenant}/graph")

# A type-scope reconcile can rewrite a large edge table: a longer budget than
# the pool's read-sized default.
TYPE_SCOPE_TIMEOUT_S = 600.0


@dataclass(frozen=True, slots=True)
class _Row:
    """One wanted edge as the property checks read it."""

    type: str
    src_id: str
    dst_id: str
    properties: dict[str, Any]


def _body_schema() -> dict[str, Any]:
    json_schema = EdgesReconcileRequest.model_json_schema()
    return {"requestBody": {
        "required": True,
        "description": (
            "JSON (`application/json`): an `EdgesReconcileRequest`, either scope. NDJSON "
            "(`application/x-ndjson`), `type` scope only: a `ReconcileTypeHeader` line, then "
            "one `ReconcileEdge` (`src_id`, `dst_id`, `props`) per line."
        ),
        "content": {
            "application/json": {"schema": json_schema},
            "application/x-ndjson": {"schema": {
                "description": "A header line, then one edge per line.",
                "oneOf": [
                    ReconcileTypeHeader.model_json_schema(),
                    ReconcileEdge.model_json_schema(),
                ],
            }},
        },
    }}


def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)


async def _read(request: Request) -> EdgesReconcileRequest:
    """The request, from a JSON body or an NDJSON one."""
    kind = request.headers.get("content-type", "application/json").split(";")[0].strip()
    if kind != "application/x-ndjson":
        try:
            return EdgesReconcileRequest.model_validate_json(await request.body())
        except ValidationError as exc:
            raise _unprocessable(str(exc)) from None
    cap = request.app.state.settings.reconcile_max_edges
    header: ReconcileTypeHeader | None = None
    edges: list[ReconcileEdge] = []
    async for line_no, text in iter_lines(request.stream()):
        try:
            if header is None:
                header = ReconcileTypeHeader.model_validate_json(text)
                continue
            edge = ReconcileEdge.model_validate_json(text)
        except ValidationError as exc:
            raise _unprocessable(f"line {line_no}: {exc}") from None
        if edge.src_id is None or edge.dst_id is None:
            raise _unprocessable(f"line {line_no}: an edge names `src_id` and `dst_id`")
        if len(edges) >= cap:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"more than GRAPHOS_RECONCILE_MAX_EDGES ({cap:,}) edges",
            )
        edges.append(edge)
    if header is None:
        raise _unprocessable("an NDJSON body starts with the header line")
    return EdgesReconcileRequest.model_construct(
        scope="type", type=header.type, src_label=header.src_label,
        dst_label=header.dst_label, edges=edges, props=header.props,
        node=None, direction="out", other_label=None,
    )


@router.post(
    "/edges:reconcile",
    response_model=EdgesReconcileResponse,
    summary="Make the stored edges of a scope exactly the given set",
    description=(
        "The caller sends the complete set of edges it wants in a scope; graphos adds the "
        "missing ones, rewrites the kept ones whose properties change and deletes the rest, in "
        "one transaction. A kept edge unchanged is not written (its revision stays).\n\n"
        "- `node` scope: the edges of `type` at `node`, in `direction`, whose other end has "
        "`other_label`; each edge names the other end by `id`.\n"
        "- `type` scope: every edge of `type` from `src_label` to `dst_label` (child labels "
        "included); JSON up to 10,000 edges, or NDJSON (a header line, then one edge per line) "
        "for larger sets.\n\n"
        "`props`: `merge` (the default) merges given properties into a kept edge's, `replace` "
        "makes them all of them. A wanted edge whose end does not exist is skipped, never "
        "created. Change records: one `upsert` for the added and changed edges and one "
        "`delete` for the removed, each with `detail.reconcile`. Reconciliations of one type "
        "queue; other types run in parallel. `dry_run=true` answers the counts and writes "
        "nothing."
    ),
    openapi_extra=_body_schema(),
    responses={
        400: {"description": (
            "An unregistered type or label, a label pair the type does not declare under "
            "`strict_endpoints`, a reserved property name, or a write the database refused."
        )},
        404: {"description": "`node` scope: no such node."},
        409: {"description": "`node` scope: the id names nodes in several child labels."},
        413: {"description": "More NDJSON edges than `GRAPHOS_RECONCILE_MAX_EDGES`."},
        422: {"description": (
            "A body that does not validate (a missing scope field, an edge without its ends), "
            "or rows the property schema refuses (strict)."
        )},
    },
)
async def reconcile_edges(  # noqa: PLR0912 — one check per rule an upsert has
    tenant: Tenant,
    request: Request,
    principal: WritePrincipal,
    dry_run: Annotated[
        bool, Query(description="Compute the difference and answer it; write nothing."),
    ] = False,
) -> EdgesReconcileResponse:
    _check_tenant(tenant, principal)
    body = await _read(request)
    spec = registered_tenants()[tenant]
    if body.type not in spec.edge_labels:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"edge type '{body.type}' is not registered for tenant '{tenant}'",
        )
    started = time.perf_counter()
    pool = request.app.state.pool
    anchor: str | None = None
    if body.scope == "node":
        assert body.node is not None and body.other_label is not None
        _check_vertex_label(tenant, body.node.label)
        _check_vertex_label(tenant, body.other_label)
        async with pool.acquire() as conn:
            try:
                node = await lookup.find_node(
                    conn, tenant=tenant, external_id=body.node.id, label=body.node.label,
                )
            except lookup.AmbiguousMatchError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"id {body.node.id!r} names nodes in labels {exc.candidates}",
                ) from exc
        if node is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no {body.node.label} node with id {body.node.id!r}",
            )
        anchor = node.gid
        out = body.direction == "out"
        src_label, dst_label = (
            (body.node.label, body.other_label) if out else (body.other_label, body.node.label)
        )
        pairs = [
            (body.node.id, str(e.id)) if out else (str(e.id), body.node.id) for e in body.edges
        ]
        scope = "out" if out else "in"
    else:
        assert body.src_label is not None and body.dst_label is not None
        _check_vertex_label(tenant, body.src_label)
        _check_vertex_label(tenant, body.dst_label)
        src_label, dst_label = body.src_label, body.dst_label
        pairs = [(str(e.src_id), str(e.dst_id)) for e in body.edges]
        scope = "type"
    refusal = policy_for(spec).refusal(body.type, src_label, dst_label)
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=refusal)
    reserved.check(*(e.props for e in body.edges))
    tally = properties.enforce(properties.edge_rows(
        properties.policies_for(spec),
        (_Row(body.type, s, d, dict(e.props)) for (s, d), e in zip(pairs, body.edges, strict=True)
         if e.props is not None),
    ))
    rows = [
        {"src_id": s, "dst_id": d, "props": e.props}
        for (s, d), e in zip(pairs, body.edges, strict=True)
    ]
    try:
        counts = await writes.reconcile_edges(
            pool, tenant=tenant, edge_type=body.type, src_label=src_label,
            dst_label=dst_label, rows=rows, scope=scope, anchor=anchor,
            props_mode=body.props, dry_run=dry_run,
            timeout_s=TYPE_SCOPE_TIMEOUT_S if scope == "type" else None,
        )
    except CypherError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"reconcile failed: {exc}",
        ) from exc
    if not dry_run:
        record_property_tally("edge", tally)
    return EdgesReconcileResponse(
        tenant=tenant, scope=body.scope, type=body.type,
        added=counts.added, changed=counts.changed, kept=counts.kept, removed=counts.removed,
        skipped=counts.skipped_count, skipped_edges=[list(p) for p in counts.skipped],
        dry_run=dry_run, elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


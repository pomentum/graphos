"""Snapshot import — `POST /v1/{tenant}/graph/snapshot-import`.

One NDJSON stream carries nodes and edges together, each line tagged with
``_kind``.  The server buffers rows per label / edge type and writes them
through the same COPY-staged SQL upserts as every other write endpoint, in
chunks of ``DEFAULT_SNAPSHOT_MERGE_CHUNK`` rows.  Buffered nodes are always
flushed before edges, so an edge never misses an endpoint that arrived
earlier in the same stream.  It is the convenient option when one file
holds a whole graph; ``/nodes:bulk`` + ``/edges:bulk`` do the same work as
two streams.

Same idempotency contract as the other write endpoints — nodes upsert on
``id``, edges on ``(src_id, dst_id, type)``; edges whose endpoints do not
exist are counted in ``edges_skipped``.  Each chunk commits on its own, so
a failure part-way leaves the earlier chunks written (re-running the same
stream is safe).

The endpoint accepts the existing per-row shape with one new key:

    {"_kind": "node", "id": "...", "label": "...", "properties": {...}}
    {"_kind": "edge", "src_id": "...", "src_label": "...",
                       "dst_id": "...", "dst_label": "...",
                       "type": "...", "properties": {...}}

Lines without `_kind` are treated as nodes (back-compat with the bulk
NDJSON shape).

`GET /v1/{tenant}/graph/snapshot` exports the tenant as such a file
(``graphos.dataset``), framed by a ``header`` line (format, version, counts,
the schema) and an ``end`` line (what was written).  The import reads them:
it refuses a header of another format or a newer version, and reports
whether the ``end`` line arrived (``complete``).  A gzip body is
decompressed, up to ``GRAPHOS_IMPORT_MAX_BYTES``.  ``?mode=empty`` imports
only into a tenant without nodes, the exact-restore path.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from graphos import dataset, properties, writes
from graphos.api.bulk import iter_lines
from graphos.api.graph import _check_tenant
from graphos.auth import ReadPrincipal, Tenant, WritePrincipal
from graphos.bootstrap import load_spec, registered_tenants
from graphos.contracts.graph import EdgeUpsert, NodeUpsert
from graphos.contracts.snapshot import (
    DEFAULT_SNAPSHOT_MERGE_CHUNK,
    MAX_SNAPSHOT_ROWS,
    SnapshotImportResponse,
)
from graphos.cypher import CypherError
from graphos.observability.audit import audit_this
from graphos.observability.metrics import record_property_tally

log = logging.getLogger(__name__)
router = APIRouter(tags=["graph"], prefix="/{tenant}/graph")

_MAX_ERROR_LINES = 50


def _tagged(kind: str, schema: str) -> dict[str, Any]:
    return {"allOf": [
        {"$ref": f"#/components/schemas/{schema}"},
        {"properties": {"_kind": {"const": kind}}},
    ]}


# One line of the stream: a node, an edge, or the header and end lines of an export.
_SNAPSHOT_LINE = {
    "description": (
        "One JSON object per line, tagged with `_kind`: `node` (a `NodeUpsert`; a line "
        "without `_kind` is a node), `edge` (an `EdgeUpsert`), and as `GET …/snapshot` "
        f"writes them a first `header` line (`format` `{dataset.FORMAT}`, `version` at most "
        f"{dataset.VERSION}) and a last `end` line."
    ),
    "anyOf": [
        _tagged("node", "NodeUpsert"),
        {**_tagged("edge", "EdgeUpsert"), "required": ["_kind"]},
        {
            "type": "object",
            "required": ["_kind"],
            "properties": {
                "_kind": {"const": "header"},
                "format": {"const": dataset.FORMAT},
                "version": {"type": "integer", "minimum": 1},
            },
        },
        {"type": "object", "required": ["_kind"], "properties": {"_kind": {"const": "end"}}},
    ],
}
_SNAPSHOT_BODY = {"requestBody": {
    "required": True,
    "description": (
        "NDJSON, or the same gzip-compressed (as `GET …/snapshot?gzip=true` exports it): a "
        "body starting with the gzip magic bytes is decompressed whatever its `Content-Type`, "
        "up to `GRAPHOS_IMPORT_MAX_BYTES`."
    ),
    "content": {
        "application/x-ndjson": {"schema": _SNAPSHOT_LINE},
        "application/gzip": {"schema": {"type": "string", "format": "binary"}},
    },
}}


@router.get(
    "/snapshot",
    summary="Export the tenant's data as one NDJSON file",
    description=(
        "Streams the tenant as the file `snapshot-import` reads (`graphos.snapshot`, "
        "version 1): a `header` line (format, version, counts, the schema), every node "
        "(`_kind: node`, each with its own label, label by label in id order), every edge "
        "(`_kind: edge`, type by type), and an `end` line with what was written; a file "
        "without it was cut off. Read in one consistent snapshot, with flat memory. "
        "`labels` / `types` narrow it (an edge only when both its ends are exported); "
        "`computed=false` leaves out `graphos_*` and `community_id`; `gzip=true` "
        "compresses it. `read`; recorded in the audit log."
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "A stream of NDJSON lines, sent as the attachment `<tenant>.ndjson` "
                "(`application/x-ndjson`), or gzip-compressed as `<tenant>.ndjson.gz` "
                "(`application/gzip`) with `gzip=true`. One JSON object per line."
            ),
            "content": {"application/x-ndjson": {}, "application/gzip": {}},
        },
        400: {"description": "A label or type in `labels` / `types` is not registered."},
    },
)
async def snapshot_export(
    tenant: Tenant,
    request: Request,
    principal: ReadPrincipal,
    labels: Annotated[list[str] | None, Query(description="Vertex labels to export.")] = None,
    types: Annotated[list[str] | None, Query(description="Edge types to export.")] = None,
    computed: Annotated[
        bool,
        Query(description="Include computed properties (`graphos_*`, `community_id`)."),
    ] = True,
    gzip: Annotated[bool, Query(description="Compress the file with gzip.")] = False,
) -> StreamingResponse:
    _check_tenant(tenant, principal)
    pool = request.app.state.pool
    spec = await load_spec(pool, tenant)
    if spec is None:   # unregistered meanwhile
        _check_tenant(tenant, principal)
        raise AssertionError("unreachable")
    for kind, asked, known in (("vertex label", labels, spec.vertex_labels),
                               ("edge type", types, spec.edge_labels)):
        unknown = sorted(set(asked or ()) - set(known))
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{kind}s not registered for tenant '{tenant}': {unknown}",
            )
    audit_this(request)
    lines = dataset.lines(pool, spec, labels=labels, types=types, computed=computed)
    name = f"{tenant}.ndjson" + (".gz" if gzip else "")
    return StreamingResponse(
        dataset.chunks(lines, gzip=gzip),
        media_type="application/gzip" if gzip else "application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


async def _body(request: Request, gunzip: dataset.Gunzip) -> AsyncIterator[bytes]:
    """The request body, decompressed when it is gzip, counted against the cap."""
    async for chunk in request.stream():
        for piece in gunzip.feed(chunk):
            yield piece
    for piece in gunzip.finish():
        yield piece


async def _is_empty(pool: asyncpg.Pool, tenant: str) -> bool:
    return not await pool.fetchval(
        f'SELECT EXISTS (SELECT 1 FROM "{tenant}"."_ag_label_vertex");',
    )


async def _restore_edge_communities(pool: asyncpg.Pool, tenant: str) -> int:
    """Set each edge's ``end_community_id`` / ``start_community_id`` from its
    ends' ``community_id``, as the community recompute's write-back does."""
    rows = await pool.fetch(
        "SELECT (v.id::text)::bigint AS gid, "
        "((v.properties)::text)::jsonb ->> 'community_id' AS cid "
        f'FROM "{tenant}"."_ag_label_vertex" v '
        "WHERE ((v.properties)::text)::jsonb ? 'community_id';",
    )
    return await writes.set_edge_end_communities(
        pool, tenant=tenant, assignments=((int(r["gid"]), str(r["cid"])) for r in rows),
    )


@router.post(
    "/snapshot-import",
    response_model=SnapshotImportResponse,
    summary="Import a whole graph from one NDJSON stream",
    description=(
        "Stream a single NDJSON body where each line is either a node "
        "(`{_kind:'node', id, label, properties}`) or an edge "
        "(`{_kind:'edge', src_id, src_label, dst_id, dst_label, type, "
        "properties}`). Lines without `_kind` are treated as nodes.\n\n"
        "Rows are written in label-grouped chunks through the same "
        "COPY-staged SQL upserts as the other write endpoints, nodes "
        "before edges, with the same idempotency contract."
    ),
    openapi_extra=_SNAPSHOT_BODY,
    responses={
        400: {"description": (
            "The database refused a write, or the stream failed part-way; the chunks written "
            "before it stay written."
        )},
        409: {
            "description": (
                "`mode=empty` and the tenant holds nodes. Or the write would give two nodes "
                "the same values of a unique property index (`application/problem+json`)."
            ),
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}},
                "application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}},
            },
        },
        413: {"description": (
            f"More than {MAX_SNAPSHOT_ROWS:,} lines, or past `GRAPHOS_IMPORT_MAX_BYTES` once "
            "decompressed; the rows read before it stay written."
        )},
        422: {"description": (
            "The request is not valid, or a `header` line of another format or a newer "
            "version."
        )},
    },
)
async def snapshot_import(  # noqa: PLR0912, PLR0915 — single-stream loop; splitting harms readability
    tenant: Tenant,
    request: Request,
    principal: WritePrincipal,
    mode: Annotated[Literal["merge", "empty"], Query(
        description=(
            "`merge`: add and merge into what the tenant holds. `empty`: refused (409) "
            "unless the tenant has no nodes, for an exact restore."
        ),
    )] = "merge",
    property_checks: Annotated[Literal["enforce", "report"], Query(
        alias="properties",
        description=(
            "`enforce`: rows are checked against the property schema as on any write. "
            "`report`: checked and counted but never refused, to restore data older than its "
            "definitions."
        ),
    )] = "enforce",
) -> SnapshotImportResponse:
    _check_tenant(tenant, principal)
    if mode == "empty" and not await _is_empty(request.app.state.pool, tenant):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"tenant '{tenant}' holds nodes; mode=empty imports only into a tenant "
                "without any (use mode=merge to add to it)"
            ),
        )

    spec = registered_tenants()[tenant]
    allowed_v = set(spec.vertex_labels)
    allowed_e = set(spec.edge_labels)
    checks = properties.policies_for(spec)
    if property_checks == "report":
        checks = checks.relaxed()
    tally = properties.Tally()

    started = time.perf_counter()
    pool = request.app.state.pool

    # Buffer rows per label / edge type until a chunk is full, then write it
    # through the COPY-staged SQL upserts in ``graphos.writes``.  Buffered
    # nodes are always flushed before edges.

    nodes_by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    edges_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    errors: list[str] = []

    nodes_received = 0
    edges_received = 0
    nodes_upserted = 0
    edges_upserted = 0
    edges_skipped = 0
    by_node_label: dict[str, int] = defaultdict(int)
    by_edge_type: dict[str, int] = defaultdict(int)

    copy_started = time.perf_counter()

    async def _flush_nodes() -> None:
        nonlocal nodes_upserted
        for label, rows in list(nodes_by_label.items()):
            if not rows:
                continue
            try:
                n = await writes.upsert_nodes(
                    pool, tenant=tenant, label=label, rows=rows,
                )
            except CypherError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"cypher error in node label '{label}': {exc}",
                ) from exc
            nodes_upserted += n
            by_node_label[label] += n
            nodes_by_label[label] = []

    async def _flush_edges() -> None:
        nonlocal edges_upserted, edges_skipped
        for (src_lbl, dst_lbl, et), rows in list(edges_by_key.items()):
            if not rows:
                continue
            try:
                merged, skipped = await writes.upsert_edges(
                    pool, tenant=tenant,
                    src_label=src_lbl, dst_label=dst_lbl, edge_type=et,
                    rows=rows,
                )
            except CypherError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"cypher error on -[{src_lbl}-{et}->{dst_lbl}]-: {exc}"
                    ),
                ) from exc
            edges_upserted += merged
            edges_skipped += skipped
            by_edge_type[et] += merged
            edges_by_key[(src_lbl, dst_lbl, et)] = []

    header: dict[str, Any] | None = None
    complete = False
    communities_in_file = False
    gunzip = dataset.Gunzip(request.app.state.settings.import_max_bytes)
    total_rows = 0
    try:
        async for line_no, text in iter_lines(_body(request, gunzip)):
            total_rows += 1
            if total_rows > MAX_SNAPSHOT_ROWS:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"snapshot exceeded {MAX_SNAPSHOT_ROWS} rows",
                )
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                if len(errors) < _MAX_ERROR_LINES:
                    errors.append(f"line {line_no}: {exc}")
                continue
            if not isinstance(payload, dict):
                if len(errors) < _MAX_ERROR_LINES:
                    errors.append(f"line {line_no}: expected a JSON object")
                continue

            kind = str(payload.pop("_kind", "node")).lower()

            if kind == "header":
                try:
                    dataset.check_header(payload)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc),
                    ) from exc
                header = {k: v for k, v in payload.items() if k != "schema"}
                continue
            if kind == "end":
                complete = True
                continue

            if kind == "node":
                nodes_received += 1
                try:
                    node = NodeUpsert.model_validate(payload)
                except ValidationError as exc:
                    if len(errors) < _MAX_ERROR_LINES:
                        errors.append(f"line {line_no}: {exc}")
                    continue
                if node.label not in allowed_v:
                    if len(errors) < _MAX_ERROR_LINES:
                        errors.append(
                            f"line {line_no}: vertex label '{node.label}' "
                            "not registered"
                        )
                    continue
                if why := tally.line(checks.for_vertex(node.label), node.properties, node.id):
                    if len(errors) < _MAX_ERROR_LINES:
                        errors.append(f"line {line_no}: {why}")
                    continue
                props = {k: v for k, v in node.properties.items() if k != "id"}
                communities_in_file = communities_in_file or "community_id" in props
                nodes_by_label[node.label].append({"id": node.id, "props": props})
                if len(nodes_by_label[node.label]) >= DEFAULT_SNAPSHOT_MERGE_CHUNK:
                    await _flush_nodes()

            elif kind == "edge":
                edges_received += 1
                try:
                    edge = EdgeUpsert.model_validate(payload)
                except ValidationError as exc:
                    if len(errors) < _MAX_ERROR_LINES:
                        errors.append(f"line {line_no}: {exc}")
                    continue
                bad_v = {edge.src_label, edge.dst_label} - allowed_v
                bad_e = {edge.type} - allowed_e
                if bad_v or bad_e:
                    if len(errors) < _MAX_ERROR_LINES:
                        parts: list[str] = []
                        if bad_v:
                            parts.append(f"unknown vertex labels {sorted(bad_v)}")
                        if bad_e:
                            parts.append(f"unknown edge type {sorted(bad_e)}")
                        errors.append(f"line {line_no}: " + "; ".join(parts))
                    continue
                if why := tally.line(checks.for_edge(edge.type), edge.properties, None):
                    if len(errors) < _MAX_ERROR_LINES:
                        errors.append(f"line {line_no}: {why}")
                    continue
                key = (edge.src_label, edge.dst_label, edge.type)
                edges_by_key[key].append({
                    "src_id": edge.src_id,
                    "dst_id": edge.dst_id,
                    "props": dict(edge.properties),
                })
                if len(edges_by_key[key]) >= DEFAULT_SNAPSHOT_MERGE_CHUNK:
                    # Flush buffered nodes first: an edge whose endpoint is
                    # still sitting in the node buffer would be MATCHed
                    # against a missing node and counted as skipped.
                    await _flush_nodes()
                    await _flush_edges()

            else:
                if len(errors) < _MAX_ERROR_LINES:
                    errors.append(
                        f"line {line_no}: unknown `_kind`: {kind!r}; expected 'node', 'edge', "
                        "'header' or 'end'"
                    )
                continue

        # Drain remaining buffers in the right order — nodes first so
        # edge endpoints exist before we try to MATCH them.
        copy_ms = int((time.perf_counter() - copy_started) * 1000)
        merge_started = time.perf_counter()
        await _flush_nodes()
        await _flush_edges()
        merge_ms = int((time.perf_counter() - merge_started) * 1000)

    except dataset.BodyTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{exc} (GRAPHOS_IMPORT_MAX_BYTES); rows read before it stay written",
        ) from exc
    except (
        HTTPException,
        asyncpg.exceptions.QueryCanceledError,
        asyncpg.exceptions.UniqueViolationError,
    ):
        raise   # → 504 / 409 via the app's timeout / unique-violation handlers
    except Exception as exc:  # noqa: BLE001
        log.warning("snapshot-import stream error", exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"snapshot import failed ({type(exc).__name__}); see server log",
        ) from exc

    # The edges' copies of their ends' communities (what community-scoped
    # walks read) are not in the file: restore them from the nodes'.
    edge_communities_set = 0
    if communities_in_file:
        edge_communities_set = await _restore_edge_communities(pool, tenant)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "snapshot import done",
        extra={
            "tenant": tenant,
            "nodes": nodes_upserted,
            "edges": edges_upserted,
            "skipped": edges_skipped,
            "elapsed_ms": elapsed_ms,
        },
    )

    record_property_tally(tenant, "snapshot", tally)
    return SnapshotImportResponse(
        tenant=tenant,
        nodes_received=nodes_received,
        edges_received=edges_received,
        nodes_upserted=nodes_upserted,
        edges_upserted=edges_upserted,
        edges_skipped=edges_skipped,
        by_node_label=dict(by_node_label),
        by_edge_type=dict(by_edge_type),
        copy_ms=copy_ms,
        merge_ms=merge_ms,
        elapsed_ms=elapsed_ms,
        errors=errors,
        mode=mode,
        header=header,
        complete=complete if header is not None else None,
        edge_communities_set=edge_communities_set,
        **tally.report(),
    )

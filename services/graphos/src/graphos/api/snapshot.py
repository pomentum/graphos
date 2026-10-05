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
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError

from graphos import writes
from graphos.api.bulk import _iter_ndjson_lines
from graphos.api.graph import _check_tenant
from graphos.api.tenants import Principal
from graphos.bootstrap import registered_tenants
from graphos.contracts.graph import EdgeUpsert, NodeUpsert
from graphos.contracts.snapshot import (
    DEFAULT_SNAPSHOT_MERGE_CHUNK,
    MAX_SNAPSHOT_ROWS,
    SnapshotImportResponse,
)
from graphos.cypher import CypherError

log = logging.getLogger(__name__)
router = APIRouter(tags=["graph"], prefix="/{tenant}/graph")

_MAX_ERROR_LINES = 50


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
)
async def snapshot_import(  # noqa: PLR0912, PLR0915 — single-stream loop; splitting harms readability
    tenant: str,
    request: Request,
    principal: Principal,
) -> SnapshotImportResponse:
    _check_tenant(tenant, principal)

    spec = registered_tenants()[tenant]
    allowed_v = set(spec.vertex_labels)
    allowed_e = set(spec.edge_labels)

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

    total_rows = 0
    try:
        async for line_no, text in _iter_ndjson_lines(request):
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
                props = {k: v for k, v in node.properties.items() if k != "id"}
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
                        f"line {line_no}: unknown `_kind`: {kind!r}; expected 'node' or 'edge'"
                    )
                continue

        # Drain remaining buffers in the right order — nodes first so
        # edge endpoints exist before we try to MATCH them.
        copy_ms = int((time.perf_counter() - copy_started) * 1000)
        merge_started = time.perf_counter()
        await _flush_nodes()
        await _flush_edges()
        merge_ms = int((time.perf_counter() - merge_started) * 1000)

    except (HTTPException, asyncpg.exceptions.QueryCanceledError):
        raise   # QueryCanceledError → 504 via the app's timeout handler
    except Exception as exc:  # noqa: BLE001
        log.warning("snapshot-import stream error", exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"snapshot import failed ({type(exc).__name__}); see server log",
        ) from exc

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
    )

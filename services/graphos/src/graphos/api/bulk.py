"""Bulk write endpoints — NDJSON streaming.

The non-bulk ``POST /v1/{tenant}/graph/{nodes,edges}`` endpoints accept JSON
bodies of up to 1000 rows. For large loads (≥1000 rows up to the
whole graph), consumers stream NDJSON to::

    POST /v1/{tenant}/graph/nodes:bulk
    POST /v1/{tenant}/graph/edges:bulk

with ``Content-Type: application/x-ndjson``. Each line is one row in the
same shape as the small-batch payload's ``nodes`` / ``edges`` element.

The server reads the request body line-by-line, validates each row with
the same Pydantic models (``NodeUpsert`` / ``EdgeUpsert``), accumulates
per-label/per-edge-key buffers up to ``BULK_FLUSH_CHUNK`` rows, and flushes
each buffer through the COPY-staged SQL writers (``graphos.cypher``).
Each flush commits on its own:
a server error part-way leaves earlier flushes written, and re-sending the
stream is safe because every write is an idempotent upsert.

Invalid rows are tracked but do not abort the stream; the first 50 errors
are surfaced in the response. This lets a consumer find and fix bad lines
without re-streaming the whole file.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError

from graphos import properties, reserved, writes
from graphos.api.graph import _check_tenant
from graphos.auth import Tenant, WritePrincipal
from graphos.bootstrap import registered_tenants
from graphos.contracts.graph import (
    BULK_FLUSH_CHUNK,
    BulkEdgesResponse,
    BulkNodesResponse,
    EdgeUpsert,
    NodeUpsert,
)
from graphos.cypher import CypherError
from graphos.endpoints import policy_for
from graphos.observability.metrics import record_property_tally

log = logging.getLogger(__name__)

router = APIRouter(tags=["graph"], prefix="/{tenant}/graph")

_MAX_ERROR_LINES = 50


async def _iter_ndjson_lines(request: Request) -> AsyncIterator[tuple[int, str]]:
    """Yield ``(line_number, line)`` from the request body, skipping blanks."""
    async for item in iter_lines(request.stream()):
        yield item


async def iter_lines(chunks: AsyncIterator[bytes]) -> AsyncIterator[tuple[int, str]]:
    """Yield ``(line_number, line)`` from a byte stream, skipping blanks.

    Buffers a partial line across chunk boundaries — NDJSON lines aren't
    guaranteed to align with HTTP chunk boundaries.
    """
    buffer = b""
    line_no = 0
    async for chunk in chunks:
        buffer += chunk
        while True:
            nl = buffer.find(b"\n")
            if nl == -1:
                break
            raw, buffer = buffer[:nl], buffer[nl + 1 :]
            line_no += 1
            text = raw.strip()
            if not text:
                continue
            yield line_no, text.decode("utf-8", errors="replace")
    # Trailing line without final newline:
    if buffer.strip():
        line_no += 1
        yield line_no, buffer.strip().decode("utf-8", errors="replace")


# ── Nodes bulk ──────────────────────────────────────────────────────────────


@router.post(
    "/nodes:bulk",
    response_model=BulkNodesResponse,
    summary="NDJSON-streamed idempotent node upsert (large loads)",
    description=(
        "POST a stream of NDJSON lines, one node per line, same shape as a "
        "single element of the small-batch `nodes` body. Server flushes in "
        f"chunks of {BULK_FLUSH_CHUNK} rows per label. Invalid lines are "
        "skipped and surfaced in `errors` (up to 50). Each flushed chunk "
        "commits on its own: a failure part-way leaves earlier chunks "
        "written, and re-sending the stream is safe (idempotent upserts)."
    ),
)
async def upsert_nodes_bulk(
    tenant: Tenant,
    request: Request,
    principal: WritePrincipal,
) -> BulkNodesResponse:
    _check_tenant(tenant, principal)

    spec = registered_tenants()[tenant]
    allowed_labels = set(spec.vertex_labels)
    checks = properties.policies_for(spec)
    tally = properties.Tally()

    started = time.perf_counter()
    pool = request.app.state.pool

    rows_received = 0
    rows_invalid = 0
    nodes_upserted = 0
    by_label_count: dict[str, int] = defaultdict(int)
    errors: list[str] = []

    buffers: dict[str, list[dict[str, Any]]] = defaultdict(list)

    async def _flush(label: str) -> None:
        nonlocal nodes_upserted
        rows = buffers.pop(label, None)
        if not rows:
            return
        try:
            n = await writes.upsert_nodes(
                pool, tenant=tenant, label=label, rows=rows,
            )
        except CypherError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"cypher error while upserting :{label}: {exc}",
            ) from exc
        nodes_upserted += n
        by_label_count[label] += n

    try:
        async for line_no, text in _iter_ndjson_lines(request):
            rows_received += 1
            try:
                payload = json.loads(text)
                node = NodeUpsert.model_validate(payload)
            except (json.JSONDecodeError, ValidationError) as exc:
                rows_invalid += 1
                if len(errors) < _MAX_ERROR_LINES:
                    errors.append(f"line {line_no}: {exc}")
                continue

            if found := reserved.reserved_keys(node.properties):
                rows_invalid += 1
                if len(errors) < _MAX_ERROR_LINES:
                    errors.append(f"line {line_no}: {reserved.refusal(found)}")
                continue

            if node.label not in allowed_labels:
                rows_invalid += 1
                if len(errors) < _MAX_ERROR_LINES:
                    errors.append(
                        f"line {line_no}: label '{node.label}' is not registered for tenant"
                    )
                continue

            if why := tally.line(checks.for_vertex(node.label), node.properties, node.id):
                rows_invalid += 1
                if len(errors) < _MAX_ERROR_LINES:
                    errors.append(f"line {line_no}: {why}")
                continue

            props = {k: v for k, v in node.properties.items() if k != "id"}
            buffers[node.label].append({"id": node.id, "props": props})

            if len(buffers[node.label]) >= BULK_FLUSH_CHUNK:
                await _flush(node.label)

        # End of stream — flush remaining buffers.
        for label in list(buffers.keys()):
            await _flush(label)

    except (
        HTTPException,
        asyncpg.exceptions.QueryCanceledError,
        asyncpg.exceptions.UniqueViolationError,
    ):
        raise   # → 504 / 409 via the app's timeout / unique-violation handlers
    except Exception as exc:  # noqa: BLE001 — surface as 400
        log.warning("nodes:bulk stream error", exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"stream error ({type(exc).__name__}); see server log",
        ) from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    record_property_tally(tenant, "node", tally)
    return BulkNodesResponse(
        tenant=tenant,
        rows_received=rows_received,
        rows_invalid=rows_invalid,
        nodes_upserted=nodes_upserted,
        by_label=dict(by_label_count),
        errors=errors,
        elapsed_ms=elapsed_ms,
        **tally.report(),
    )


# ── Edges bulk ──────────────────────────────────────────────────────────────


def _validate_edge_labels(
    edge: EdgeUpsert,
    allowed_v: set[str],
    allowed_e: set[str],
) -> str | None:
    """Return a human-readable error string, or None if the edge is OK."""
    bad_v = {edge.src_label, edge.dst_label} - allowed_v
    bad_e = {edge.type} - allowed_e
    if not bad_v and not bad_e:
        return None
    parts: list[str] = []
    if bad_v:
        parts.append(f"unknown vertex labels {sorted(bad_v)}")
    if bad_e:
        parts.append(f"unknown edge type {sorted(bad_e)}")
    return "; ".join(parts)


@router.post(
    "/edges:bulk",
    response_model=BulkEdgesResponse,
    summary="NDJSON-streamed idempotent edge upsert (large loads)",
    description=(
        "Stream NDJSON edges, same shape as a single element of the small-"
        f"batch `edges` body. Flushes per (src_label, dst_label, type) in "
        f"chunks of {BULK_FLUSH_CHUNK} rows. Missing-endpoint rows are "
        "counted in `edges_skipped` (same as the small-batch endpoint)."
    ),
)
async def upsert_edges_bulk(  # noqa: PLR0915 — streaming loop, splitting further harms readability
    tenant: Tenant,
    request: Request,
    principal: WritePrincipal,
) -> BulkEdgesResponse:
    _check_tenant(tenant, principal)

    spec = registered_tenants()[tenant]
    allowed_v = set(spec.vertex_labels)
    allowed_e = set(spec.edge_labels)
    # With strict_endpoints, a line of a declared type outside its pairs is invalid.
    policy = policy_for(spec)
    checks = properties.policies_for(spec)
    tally = properties.Tally()

    started = time.perf_counter()
    pool = request.app.state.pool

    rows_received = 0
    rows_invalid = 0
    edges_upserted = 0
    edges_skipped = 0
    by_type_count: dict[str, int] = defaultdict(int)
    errors: list[str] = []

    buffers: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)

    async def _flush(key: tuple[str, str, str]) -> None:
        nonlocal edges_upserted, edges_skipped
        rows = buffers.pop(key, None)
        if not rows:
            return
        src_lbl, dst_lbl, et = key
        try:
            merged, skipped = await writes.upsert_edges(
                pool,
                tenant=tenant,
                src_label=src_lbl,
                dst_label=dst_lbl,
                edge_type=et,
                rows=rows,
            )
        except CypherError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"cypher error on -[{src_lbl}-{et}->{dst_lbl}]-: {exc}",
            ) from exc
        edges_upserted += merged
        edges_skipped += skipped
        by_type_count[et] += merged

    try:
        async for line_no, text in _iter_ndjson_lines(request):
            rows_received += 1
            try:
                payload = json.loads(text)
                edge = EdgeUpsert.model_validate(payload)
            except (json.JSONDecodeError, ValidationError) as exc:
                rows_invalid += 1
                if len(errors) < _MAX_ERROR_LINES:
                    errors.append(f"line {line_no}: {exc}")
                continue

            found = reserved.reserved_keys(edge.properties)
            err = (
                (reserved.refusal(found) if found else None)
                or _validate_edge_labels(edge, allowed_v, allowed_e)
                or policy.refusal(edge.type, edge.src_label, edge.dst_label)
                or tally.line(checks.for_edge(edge.type), edge.properties, None)
            )
            if err is not None:
                rows_invalid += 1
                if len(errors) < _MAX_ERROR_LINES:
                    errors.append(f"line {line_no}: {err}")
                continue

            key = (edge.src_label, edge.dst_label, edge.type)
            buffers[key].append(
                {"src_id": edge.src_id, "dst_id": edge.dst_id, "props": dict(edge.properties)}
            )

            if len(buffers[key]) >= BULK_FLUSH_CHUNK:
                await _flush(key)

        for key in list(buffers.keys()):
            await _flush(key)

    except (
        HTTPException,
        asyncpg.exceptions.QueryCanceledError,
        asyncpg.exceptions.UniqueViolationError,
    ):
        raise   # → 504 / 409 via the app's timeout / unique-violation handlers
    except Exception as exc:  # noqa: BLE001 — surface as 400
        log.warning("edges:bulk stream error", exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"stream error ({type(exc).__name__}); see server log",
        ) from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    record_property_tally(tenant, "edge", tally)
    return BulkEdgesResponse(
        tenant=tenant,
        rows_received=rows_received,
        rows_invalid=rows_invalid,
        edges_upserted=edges_upserted,
        edges_skipped=edges_skipped,
        by_type=dict(by_type_count),
        errors=errors,
        elapsed_ms=elapsed_ms,
        **tally.report(),
    )

"""Bulk write endpoints — NDJSON streaming.

The non-bulk ``POST /v1/{tenant}/graph/{nodes,edges}`` endpoints accept JSON
bodies of up to 1000 rows. For large loads (≥1000 rows up to the
whole graph), consumers stream NDJSON to::

    POST /v1/{tenant}/graph/nodes:bulk
    POST /v1/{tenant}/graph/edges:bulk

with ``Content-Type: application/x-ndjson``. Each line is one row in the
same shape as the small-batch payload's ``nodes`` / ``edges`` element.

The server reads the request body line-by-line, validates each row with
the same Pydantic models (``NodeUpsert`` / ``EdgeUpsert``) and hands it to a
:class:`NodeWriter` / :class:`EdgeWriter`, which check it as every write is
checked (reserved names, registered labels, declared pairs, the property
definitions) and write per-label/per-edge-key buffers of up to
``BULK_FLUSH_CHUNK`` rows through the COPY-staged SQL writers
(``graphos.writes``).  Each flush commits on its own:
a server error part-way leaves earlier flushes written, and re-sending the
stream is safe because every write is an idempotent upsert.  The CSV
endpoints (``graphos.api.csv_import``) write through the same writers.

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

from graphos import lookup, properties, reserved, writes
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


class _Writer:
    """What the node and edge writers share: counts, errors, the tally.

    ``where`` names a row in an error: ``line N`` of a stream, ``row N`` of a
    CSV file.  With ``dry_run`` every row is checked and nothing is written:
    ``written`` counts the rows that would be.
    """

    def __init__(self, request: Request, tenant: str, *, dry_run: bool = False) -> None:
        self.tenant = tenant
        self.dry_run = dry_run
        self.pool = request.app.state.pool
        self.spec = registered_tenants()[tenant]
        self.checks = properties.policies_for(self.spec)
        self.tally = properties.Tally()
        self.received = 0
        self.invalid = 0
        self.written = 0
        self.by_name: dict[str, int] = defaultdict(int)
        self.errors: list[str] = []

    def refuse(self, where: str, why: str) -> None:
        """Counts a row that is not written, and keeps the first errors."""
        self.invalid += 1
        if len(self.errors) < _MAX_ERROR_LINES:
            self.errors.append(f"{where}: {why}")


class NodeWriter(_Writer):
    """Checks node rows as every write is checked and writes them in batches."""

    def __init__(self, request: Request, tenant: str, *, dry_run: bool = False) -> None:
        super().__init__(request, tenant, dry_run=dry_run)
        self._labels = set(self.spec.vertex_labels)
        self._buffers: dict[str, list[dict[str, Any]]] = defaultdict(list)

    async def add(self, where: str, node: NodeUpsert) -> None:
        self.received += 1
        if found := reserved.reserved_keys(node.properties):
            self.refuse(where, reserved.refusal(found))
        elif node.label not in self._labels:
            self.refuse(where, f"label '{node.label}' is not registered for tenant")
        elif why := self.tally.line(self.checks.for_vertex(node.label), node.properties, node.id):
            self.refuse(where, why)
        else:
            props = {k: v for k, v in node.properties.items() if k != "id"}
            self._buffers[node.label].append({"id": node.id, "props": props})
            if len(self._buffers[node.label]) >= BULK_FLUSH_CHUNK:
                await self._flush(node.label)

    async def close(self) -> None:
        """Writes what is still buffered."""
        for label in list(self._buffers):
            await self._flush(label)
        record_property_tally("node", self.tally)

    async def _flush(self, label: str) -> None:
        rows = self._buffers.pop(label, None)
        if not rows:
            return
        if self.dry_run:
            n = len({row["id"] for row in rows})
        else:
            try:
                n = await writes.upsert_nodes(self.pool, tenant=self.tenant, label=label, rows=rows)
            except CypherError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"cypher error while upserting :{label}: {exc}",
                ) from exc
        self.written += n
        self.by_name[label] += n


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


class EdgeWriter(_Writer):
    """Checks edge rows as every write is checked and writes them in batches.

    An edge whose end does not exist is not written: counted in ``skipped``
    (in a dry run too, by looking the ends up).
    """

    def __init__(self, request: Request, tenant: str, *, dry_run: bool = False) -> None:
        super().__init__(request, tenant, dry_run=dry_run)
        self._vertices = set(self.spec.vertex_labels)
        self._types = set(self.spec.edge_labels)
        # With strict_endpoints, a row of a declared type outside its pairs is invalid.
        self._pairs = policy_for(self.spec)
        self._buffers: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        self.skipped = 0

    async def add(self, where: str, edge: EdgeUpsert) -> None:
        self.received += 1
        found = reserved.reserved_keys(edge.properties)
        err = (
            (reserved.refusal(found) if found else None)
            or _validate_edge_labels(edge, self._vertices, self._types)
            or self._pairs.refusal(edge.type, edge.src_label, edge.dst_label)
            or self.tally.line(self.checks.for_edge(edge.type), edge.properties, None)
        )
        if err is not None:
            self.refuse(where, err)
            return
        key = (edge.src_label, edge.dst_label, edge.type)
        self._buffers[key].append(
            {"src_id": edge.src_id, "dst_id": edge.dst_id, "props": dict(edge.properties)}
        )
        if len(self._buffers[key]) >= BULK_FLUSH_CHUNK:
            await self._flush(key)

    async def close(self) -> None:
        """Writes what is still buffered."""
        for key in list(self._buffers):
            await self._flush(key)
        record_property_tally("edge", self.tally)

    async def _flush(self, key: tuple[str, str, str]) -> None:
        rows = self._buffers.pop(key, None)
        if not rows:
            return
        src_lbl, dst_lbl, et = key
        if self.dry_run:
            merged, skipped = await self._would_write(src_lbl, dst_lbl, rows)
        else:
            try:
                merged, skipped = await writes.upsert_edges(
                    self.pool, tenant=self.tenant, src_label=src_lbl, dst_label=dst_lbl,
                    edge_type=et, rows=rows,
                )
            except CypherError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"cypher error on -[{src_lbl}-{et}->{dst_lbl}]-: {exc}",
                ) from exc
        self.written += merged
        self.skipped += skipped
        self.by_name[et] += merged

    async def _would_write(
        self, src_label: str, dst_label: str, rows: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """What ``upsert_edges`` would do: the distinct pairs whose two ends
        exist, and the rows skipped for a missing end."""
        async with self.pool.acquire() as conn:
            srcs = await lookup.existing_ids(
                conn, self.tenant, src_label, sorted({r["src_id"] for r in rows}),
            )
            dsts = await lookup.existing_ids(
                conn, self.tenant, dst_label, sorted({r["dst_id"] for r in rows}),
            )
        present = [r for r in rows if r["src_id"] in srcs and r["dst_id"] in dsts]
        return len({(r["src_id"], r["dst_id"]) for r in present}), len(rows) - len(present)


def stream_error(endpoint: str, exc: Exception) -> HTTPException:
    """An unexpected failure part-way through a body, answered 400."""
    log.warning("%s stream error", endpoint, exc_info=exc)
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"stream error ({type(exc).__name__}); see server log",
    )


# Passed through to the app's handlers: 504 (timeout), 409 (unique violation).
PASSED_THROUGH = (
    HTTPException,
    asyncpg.exceptions.QueryCanceledError,
    asyncpg.exceptions.UniqueViolationError,
)


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
    writer = NodeWriter(request, tenant)
    started = time.perf_counter()
    try:
        async for line_no, text in _iter_ndjson_lines(request):
            try:
                node = NodeUpsert.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                writer.received += 1
                writer.refuse(f"line {line_no}", str(exc))
                continue
            await writer.add(f"line {line_no}", node)
        await writer.close()
    except PASSED_THROUGH:
        raise
    except Exception as exc:  # noqa: BLE001 — surface as 400
        raise stream_error("nodes:bulk", exc) from exc

    return BulkNodesResponse(
        tenant=tenant,
        rows_received=writer.received,
        rows_invalid=writer.invalid,
        nodes_upserted=writer.written,
        by_label=dict(writer.by_name),
        errors=writer.errors,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        **writer.tally.report(),
    )


# ── Edges bulk ──────────────────────────────────────────────────────────────


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
async def upsert_edges_bulk(
    tenant: Tenant,
    request: Request,
    principal: WritePrincipal,
) -> BulkEdgesResponse:
    _check_tenant(tenant, principal)
    writer = EdgeWriter(request, tenant)
    started = time.perf_counter()
    try:
        async for line_no, text in _iter_ndjson_lines(request):
            try:
                edge = EdgeUpsert.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                writer.received += 1
                writer.refuse(f"line {line_no}", str(exc))
                continue
            await writer.add(f"line {line_no}", edge)
        await writer.close()
    except PASSED_THROUGH:
        raise
    except Exception as exc:  # noqa: BLE001 — surface as 400
        raise stream_error("edges:bulk", exc) from exc

    return BulkEdgesResponse(
        tenant=tenant,
        rows_received=writer.received,
        rows_invalid=writer.invalid,
        edges_upserted=writer.written,
        edges_skipped=writer.skipped,
        by_type=dict(writer.by_name),
        errors=writer.errors,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        **writer.tally.report(),
    )

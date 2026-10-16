"""CSV import — ``POST /v1/{tenant}/graph/nodes:csv`` and ``…/edges:csv``.

The body is the CSV file itself (``Content-Type: text/csv``); the query
says what its rows are: nodes of a label, or relationships of a type
between two labels, which columns hold the ids, and, in ``columns`` (a JSON
object, header → ``{"property": name | null, "type": ...}``), how the other
columns map to properties.  Without ``columns`` every other column is a
property named after its header, typed after its definition.  How the file
is read and its rows merged is ``graphos.csv_rows``.

The records are written through the same writers as ``nodes:bulk`` /
``edges:bulk`` (``graphos.api.bulk``): every check a write gets (reserved
names, registered labels, declared pairs, the property definitions), the
same batches, change records and revisions.  So, as there, each batch
commits on its own, and sending the same file again is safe (upserts).
``dry_run=true`` checks every row and writes nothing: the answer is what the
import would do.

The rows are held in memory to merge repeated ids: a file is at most
``GRAPHOS_CSV_MAX_BYTES`` (413 past it).
"""

from __future__ import annotations

import json
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import ValidationError

from graphos import csv_rows
from graphos.api.bulk import PASSED_THROUGH, EdgeWriter, NodeWriter, _Writer, stream_error
from graphos.api.graph import UNIQUE_VIOLATION_RESPONSE, _check_tenant, _check_vertex_label
from graphos.auth import Tenant, WritePrincipal
from graphos.bootstrap import registered_tenants
from graphos.contracts.graph import (
    CsvColumnPlan,
    CsvImportResponse,
    CsvIssue,
    EdgeUpsert,
    NodeUpsert,
)
from graphos.endpoints import policy_for

router = APIRouter(tags=["graph"], prefix="/{tenant}/graph")

Delimiter = Annotated[
    Literal["comma", "semicolon", "tab"] | None,
    Query(description="The separator; detected from the first lines when left out."),
]
Columns = Annotated[
    str | None,
    Query(
        description=(
            'JSON: header → {"property": name or null (left out), "type": "auto" | "text" | '
            '"number" | "boolean"}. Columns not named are properties named after their header.'
        ),
    ),
]
DryRun = Annotated[bool, Query(description="Check every row and write nothing.")]

_DESCRIPTION = (
    "The body is the CSV file (UTF-8, comma, semicolon or tab separated, the first row the "
    "header). Rows repeating an id are merged into one record, in the file's order (a later "
    "non-empty cell wins). Every record is checked as `{bulk}` checks a line and written "
    "through it, in batches that commit on their own. `dry_run=true` writes nothing and says "
    "what the import would do. Errors name the file's rows, counted from the first one under "
    "the header."
)

_CSV_BODY = {"requestBody": {
    "required": True,
    "content": {"text/csv": {"schema": {
        "type": "string",
        "format": "binary",
        "description": (
            "The CSV file: UTF-8, comma, semicolon or tab separated (`delimiter`), the first "
            "row the header; at most `GRAPHOS_CSV_MAX_BYTES`."
        ),
    }}},
}}


def _csv_responses(registered: str, refused: str = "") -> dict[int | str, dict[str, Any]]:
    """The errors of a CSV import: ``registered`` names what must be registered,
    ``refused`` what else the import refuses with 422."""
    return {
        400: {"description": (
            f"{registered} not registered for the tenant, a file that cannot be read as CSV "
            "(not UTF-8, empty), or a write the database refused or that failed part-way (the "
            "batches written before it stay written)."
        )},
        413: {"description": "The file is larger than `GRAPHOS_CSV_MAX_BYTES`."},
        422: {"description": (
            "The request is not valid: `columns` is not a JSON column map or does not fit the "
            "header (an unknown column, a bad or reserved property name, an unknown type, two "
            f"columns for one property), or an id column is not in the header{refused}."
        )},
    }


def _choices(columns: str | None) -> dict[str, dict[str, Any]]:
    if columns is None:
        return {}
    try:
        parsed = json.loads(columns)
    except json.JSONDecodeError as exc:
        raise _unprocessable(f"columns is not JSON: {exc}") from exc
    ok = isinstance(parsed, dict) and all(
        isinstance(v, dict) and set(v) <= {"property", "type"} for v in parsed.values()
    )
    if not ok:
        raise _unprocessable('columns must map headers to {"property": ..., "type": ...}')
    return parsed


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def _table(request: Request, delimiter: str | None) -> csv_rows.Table:
    limit = request.app.state.settings.csv_max_bytes
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"the file is larger than {limit} bytes (GRAPHOS_CSV_MAX_BYTES)",
            )
    try:
        chosen = csv_rows.DELIMITERS[delimiter] if delimiter else None
        return csv_rows.read_table(bytes(data), chosen)
    except csv_rows.CsvError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _invalid(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in exc.errors()
    )


def _answer(
    writer: _Writer, table: csv_rows.Table, records: int, columns: list[csv_rows.Column],
    issues: csv_rows.Issues, started: float, skipped: int = 0,
) -> CsvImportResponse:
    name = {v: k for k, v in csv_rows.DELIMITERS.items()}[table.delimiter]
    return CsvImportResponse(
        tenant=writer.tenant,
        dry_run=writer.dry_run,
        delimiter=name,  # type: ignore[arg-type]
        rows=len(table.rows),
        records=records,
        rows_invalid=writer.invalid,
        written=writer.written,
        edges_skipped=skipped,
        columns=[CsvColumnPlan(header=c.header, property=c.property, type=c.type) for c in columns],
        issues=[
            CsvIssue(code=i.code, refused=i.refused, count=i.count, examples=i.examples)
            for i in issues.listed()
        ],
        errors=writer.errors,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        **writer.tally.report(),
    )


@router.post(
    "/nodes:csv",
    response_model=CsvImportResponse,
    summary="Import a CSV file as nodes of a label",
    description=_DESCRIPTION.format(bulk="nodes:bulk"),
    openapi_extra=_CSV_BODY,
    responses={**_csv_responses("`label`"), **UNIQUE_VIOLATION_RESPONSE},
)
async def import_nodes_csv(  # noqa: PLR0913 — FastAPI route parameters
    tenant: Tenant,
    request: Request,
    principal: WritePrincipal,
    label: Annotated[str, Query(description="The vertex label of every row.")],
    id: Annotated[  # noqa: A002 — the query parameter's name
        str | None,
        Query(description="The id column's header; `id` (or `key`) when left out, else the first."),
    ] = None,
    columns: Columns = None,
    delimiter: Delimiter = None,
    dry_run: DryRun = False,
) -> CsvImportResponse:
    _check_tenant(tenant, principal)
    _check_vertex_label(tenant, label)
    choices = _choices(columns)
    started = time.perf_counter()
    writer = NodeWriter(request, tenant, dry_run=dry_run)
    table = await _table(request, delimiter)
    declared = {n: d.spec for n, d in writer.checks.for_vertex(label).properties.items()}
    try:
        key = csv_rows.id_column(table.header, id)
        plan = csv_rows.plan_columns(table.header, [key], choices, declared, nodes=True)
    except csv_rows.CsvError as exc:
        raise _unprocessable(str(exc)) from exc
    issues = csv_rows.Issues()
    records, keyless = csv_rows.merge_rows(table, [key], plan, issues, nodes=True)
    try:
        for record in sorted([*records, *keyless], key=lambda r: r.row):
            where = f"row {record.row}"
            try:
                node = NodeUpsert.model_validate(
                    {"id": record.ids[0], "label": label, "properties": record.properties},
                )
            except ValidationError as exc:
                writer.received += 1
                writer.refuse(where, _invalid(exc))
                continue
            await writer.add(where, node)
        await writer.close()
    except PASSED_THROUGH:
        raise
    except Exception as exc:  # noqa: BLE001 — surface as 400
        raise stream_error("nodes:csv", exc) from exc
    return _answer(writer, table, len(records) + len(keyless), plan, issues, started)


@router.post(
    "/edges:csv",
    response_model=CsvImportResponse,
    summary="Import a CSV file as relationships of a type",
    description=_DESCRIPTION.format(bulk="edges:bulk")
    + " A relationship whose end does not exist is counted in `edges_skipped`.",
    openapi_extra=_CSV_BODY,
    responses=_csv_responses(
        "`type`, `src_label` or `dst_label`",
        "; or the source and target are one column, or the type does not declare the label "
        "pair (`strict_endpoints`)",
    ),
)
async def import_edges_csv(  # noqa: PLR0913 — FastAPI route parameters
    tenant: Tenant,
    request: Request,
    principal: WritePrincipal,
    type: Annotated[str, Query(description="The relationship type of every row.")],  # noqa: A002
    src_label: Annotated[str, Query(description="The label of the source nodes.")],
    dst_label: Annotated[str, Query(description="The label of the target nodes.")],
    src: Annotated[
        str | None,
        Query(description="The source id column's header; `src`, `source`, `from`… when left out."),
    ] = None,
    dst: Annotated[
        str | None,
        Query(description="The target id column's header; `dst`, `target`, `to`… when left out."),
    ] = None,
    columns: Columns = None,
    delimiter: Delimiter = None,
    dry_run: DryRun = False,
) -> CsvImportResponse:
    _check_tenant(tenant, principal)
    _check_vertex_label(tenant, src_label)
    _check_vertex_label(tenant, dst_label)
    spec = registered_tenants()[tenant]
    if type not in spec.edge_labels:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"edge type '{type}' is not registered for tenant '{tenant}'",
        )
    pairs = policy_for(spec)
    if refusal := pairs.refusal(type, src_label, dst_label):
        raise _unprocessable(refusal)
    choices = _choices(columns)
    started = time.perf_counter()
    writer = EdgeWriter(request, tenant, dry_run=dry_run)
    table = await _table(request, delimiter)
    declared = {n: d.spec for n, d in writer.checks.for_edge(type).properties.items()}
    try:
        keys = list(csv_rows.end_columns(table.header, src, dst))
        if keys[0] == keys[1]:
            same = table.header[keys[0]]
            raise csv_rows.CsvError(f"the source and target are the same column {same!r}")
        plan = csv_rows.plan_columns(table.header, keys, choices, declared, nodes=False)
    except csv_rows.CsvError as exc:
        raise _unprocessable(str(exc)) from exc
    issues = csv_rows.Issues()
    if not pairs.allows(type, src_label, dst_label):
        issues.add("undeclared_pair", refused=False, value=f"{src_label} → {type} → {dst_label}")
    records, keyless = csv_rows.merge_rows(table, keys, plan, issues, nodes=False)
    try:
        for record in sorted([*records, *keyless], key=lambda r: r.row):
            where = f"row {record.row}"
            try:
                edge = EdgeUpsert.model_validate({
                    "src_id": record.ids[0], "src_label": src_label,
                    "dst_id": record.ids[1], "dst_label": dst_label,
                    "type": type, "properties": record.properties,
                })
            except ValidationError as exc:
                writer.received += 1
                writer.refuse(where, _invalid(exc))
                continue
            await writer.add(where, edge)
        await writer.close()
    except PASSED_THROUGH:
        raise
    except Exception as exc:  # noqa: BLE001 — surface as 400
        raise stream_error("edges:csv", exc) from exc
    return _answer(
        writer, table, len(records) + len(keyless), plan, issues, started, writer.skipped,
    )

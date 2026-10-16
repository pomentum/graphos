"""Operational endpoints: postgres-side introspection and maintenance.

These endpoints are admin-scoped (token has ``admin: true``) and operate
on the data plane via plain SQL — no Cypher. They expose what every
postgres operator would otherwise reach for via ``psql`` against
``graphos-db``:

    GET  /v1/db/stats          — table sizes, dead-tuple counts, last vacuum.
    GET  /v1/db/slow-queries   — top-N from pg_stat_statements.
    POST /v1/db/vacuum         — VACUUM (ANALYZE) on chosen tables.

The point isn't to wrap every pg admin command in a REST endpoint — it's
to give the on-call rotation a structured, audit-logged way to ask the
two or three operational questions they'd actually want to answer
without dropping into ``docker exec graphos-db psql``.
"""

from __future__ import annotations

import re
import time
from typing import Annotated

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from graphos.api.jobs import (
    ACCEPTED_RESPONSE,
    CANCELLED_RESPONSE,
    respond_async,
    run_sync,
    wants_async,
)
from graphos.auth import AdminPrincipal
from graphos.contracts.db import (
    DbStatsResponse,
    SlowQueriesResponse,
    SlowQuery,
    TableStat,
    VacuumRequest,
    VacuumResponse,
)
from graphos.db import allow_long_statements

_VACUUM_TIMEOUT_S = 3600.0

router = APIRouter(tags=["db"], prefix="/db")

# Whitelist of identifier characters for table names supplied to /vacuum.
# We do NOT interpolate untrusted strings into VACUUM commands; the regex
# is the gate, and any unmatched identifier raises 422.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_QUALIFIED_IDENT_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]{0,62})\.([A-Za-z_][A-Za-z0-9_]{0,62})$"
)


# ── /v1/db/stats ───────────────────────────────────────────────────────────


@router.get(
    "/stats",
    response_model=DbStatsResponse,
    summary="Database stats: sizes, dead-tuple counts, vacuum timestamps",
    description=(
        "Admin only. The PostgreSQL and Apache AGE versions, the database size, and the 200 "
        "largest tables (every tenant's and graphos's own) with their live and dead tuple "
        "counts, total and index sizes, and when they were last vacuumed, autovacuumed and "
        "analyzed."
    ),
)
async def db_stats(request: Request, principal: AdminPrincipal) -> DbStatsResponse:

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        pg_version = await conn.fetchval("SHOW server_version;")
        db_size = await conn.fetchval("SELECT pg_database_size(current_database())")
        age_version = await conn.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'age'"
        )
        rows = await conn.fetch(
            """
            SELECT
              s.schemaname              AS schema_name,
              s.relname                 AS table_name,
              s.n_live_tup              AS n_live_tuples,
              s.n_dead_tup              AS n_dead_tuples,
              pg_total_relation_size(c.oid) AS total_bytes,
              pg_indexes_size(c.oid)        AS index_bytes,
              s.last_vacuum,
              s.last_autovacuum,
              s.last_analyze
            FROM pg_stat_user_tables s
            JOIN pg_class c
              ON c.relname = s.relname
             AND c.relnamespace = (
                   SELECT oid FROM pg_namespace WHERE nspname = s.schemaname
                 )
            ORDER BY pg_total_relation_size(c.oid) DESC
            LIMIT 200;
            """
        )

    return DbStatsResponse(
        postgres_version=str(pg_version),
        database_size_bytes=int(db_size),
        age_version=str(age_version) if age_version else None,
        tables=[
            TableStat(
                schema_name=r["schema_name"],
                table_name=r["table_name"],
                n_live_tuples=r["n_live_tuples"] or 0,
                n_dead_tuples=r["n_dead_tuples"] or 0,
                total_bytes=r["total_bytes"] or 0,
                index_bytes=r["index_bytes"] or 0,
                last_vacuum=r["last_vacuum"].isoformat() if r["last_vacuum"] else None,
                last_autovacuum=(
                    r["last_autovacuum"].isoformat() if r["last_autovacuum"] else None
                ),
                last_analyze=r["last_analyze"].isoformat() if r["last_analyze"] else None,
            )
            for r in rows
        ],
    )


# ── /v1/db/slow-queries ────────────────────────────────────────────────────


@router.get(
    "/slow-queries",
    response_model=SlowQueriesResponse,
    summary="Top slow queries from pg_stat_statements",
    description=(
        "Admin only. The statements with the highest mean execution time, with their calls, "
        "total and mean time and rows. `available` is false, with a `note`, when the "
        "`pg_stat_statements` extension is not installed."
    ),
)
async def slow_queries(
    request: Request,
    principal: AdminPrincipal,
    limit: Annotated[
        int, Query(ge=1, le=200, description="How many statements to return."),
    ] = 25,
) -> SlowQueriesResponse:

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        present = await conn.fetchval(
            "SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'"
        )
        if not present:
            return SlowQueriesResponse(
                available=False,
                queries=[],
                note=(
                    "pg_stat_statements extension not installed. Add it to "
                    "shared_preload_libraries in graphos-db's compose env "
                    "(already configured in the bundled compose file) and "
                    "`CREATE EXTENSION pg_stat_statements` from the init "
                    "script."
                ),
            )

        rows = await conn.fetch(
            """
            SELECT
              query,
              calls,
              total_exec_time   AS total_exec_ms,
              mean_exec_time    AS mean_exec_ms,
              rows
            FROM pg_stat_statements
            ORDER BY mean_exec_time DESC
            LIMIT $1;
            """,
            limit,
        )

    return SlowQueriesResponse(
        available=True,
        queries=[
            SlowQuery(
                query=str(r["query"]),
                calls=int(r["calls"]),
                total_exec_ms=float(r["total_exec_ms"]),
                mean_exec_ms=float(r["mean_exec_ms"]),
                rows=int(r["rows"]),
            )
            for r in rows
        ],
    )


# ── /v1/db/vacuum ──────────────────────────────────────────────────────────


def _validated_table_names(tables: list[str] | None) -> list[str]:
    """Whitelist-check each table identifier before interpolating into VACUUM.

    Each entry must match either `<table>` or `<schema>.<table>`, both
    using the standard postgres identifier pattern. Anything else raises
    HTTPException 422.
    """
    if not tables:
        return []
    out: list[str] = []
    for t in tables:
        if "." in t:
            m = _QUALIFIED_IDENT_RE.fullmatch(t)
            if not m:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"invalid qualified table identifier: {t!r}",
                )
            out.append(f'"{m.group(1)}"."{m.group(2)}"')
        else:
            if not _IDENT_RE.fullmatch(t):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"invalid table identifier: {t!r}",
                )
            out.append(f'"{t}"')
    return out


@router.post(
    "/vacuum",
    response_model=VacuumResponse,
    summary="Manual VACUUM (ANALYZE / FULL optional)",
    description=(
        "Without `tables`: VACUUM the whole database. With `tables`: VACUUM "
        "only the named ones (whitelist-validated). Use `full=true` "
        "sparingly — it takes an exclusive lock. With `Prefer: "
        "respond-async` it runs as a background job (202, see `/v1/jobs`)."
    ),
    responses={**ACCEPTED_RESPONSE, **CANCELLED_RESPONSE},
)
async def vacuum(
    body: VacuumRequest,
    request: Request,
    principal: AdminPrincipal,
) -> VacuumResponse | JSONResponse:
    _validated_table_names(body.tables)   # reject bad names before queueing
    if wants_async(request):
        return await respond_async(
            request, kind="vacuum", tenant=None, body=body, principal=principal,
        )
    return await run_sync(
        request, kind="vacuum", tenant=None, body=body, principal=principal,
        run=lambda: run_vacuum(request.app.state.pool, body),
    )


async def run_vacuum(pool: asyncpg.Pool, body: VacuumRequest) -> VacuumResponse:
    """The vacuum itself, for the endpoint and for background jobs."""
    target_idents = _validated_table_names(body.tables)
    parts: list[str] = ["VACUUM"]
    options: list[str] = []
    if body.full:
        options.append("FULL")
    if body.analyze:
        options.append("ANALYZE")
    if options:
        parts.append("(" + ", ".join(options) + ")")

    started = time.perf_counter()

    # VACUUM cannot run inside a transaction; asyncpg's default behaviour
    # is one statement = one implicit transaction, so we use a dedicated
    # connection and execute outside a transaction context.
    # Vacuum is maintenance, not a query: give it an hour, server- and
    # client-side, instead of the request statement timeout.
    async with pool.acquire() as conn:
        await allow_long_statements(conn, _VACUUM_TIMEOUT_S)
        if target_idents:
            for ident in target_idents:
                await conn.execute(" ".join(parts) + f" {ident};", timeout=_VACUUM_TIMEOUT_S)
        else:
            await conn.execute(" ".join(parts) + ";", timeout=_VACUUM_TIMEOUT_S)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return VacuumResponse(
        tables_processed=body.tables or [],
        elapsed_ms=elapsed_ms,
    )

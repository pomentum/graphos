"""The Cypher console: ``/v1/{tenant}/cypher`` (see :mod:`graphos.console`).

- ``POST /v1/{tenant}/cypher``                     run a read-only query
- ``GET  /v1/{tenant}/cypher/saved``               the tenant's saved queries
- ``PUT  /v1/{tenant}/cypher/saved/{name}``        save one (``maintain``)
- ``DELETE /v1/{tenant}/cypher/saved/{name}``      delete one (``maintain``)
- ``POST /v1/{tenant}/cypher/saved/{name}:run``    run one with its parameters

Every route answers 404 while the console is off (``GRAPHOS_CYPHER``).
Running and listing need ``GRAPHOS_CYPHER_LEVEL`` on the tenant (``read`` by
default).  Each run is written to the audit log, and logged with its text.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request, status

from graphos.auth import MaintainPrincipal, ReadPrincipal, Tenant, require_level
from graphos.console import Console, ConsoleError
from graphos.contracts.cypher import (
    SAVED_NAME,
    CypherRequest,
    CypherResult,
    SavedQuery,
    SavedQueryBody,
    SavedQueryRun,
)
from graphos.identity import Level, Principal
from graphos.observability.audit import audit_this

log = logging.getLogger(__name__)

router = APIRouter(tags=["cypher"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"description": "AGE rejected the query (its message), or it tried to write."},
    404: {"description": "The console is off, or no such saved query."},
    429: {"description": "You are running a query already, or the console is busy."},
    504: {"description": "The query ran past the console's timeout and was stopped."},
}

SavedName = Annotated[str, Path(
    pattern=SAVED_NAME,
    description="The saved query's name: a letter or digit, then up to 62 of those, `_` or `-`.",
)]


def _console(request: Request, principal: Principal, tenant: str) -> Console:
    console: Console | None = getattr(request.app.state, "console", None)
    if console is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="the Cypher console is off",
        )
    require_level(principal, tenant, Level.parse(console.settings.cypher_level))
    return console


async def _run(  # noqa: PLR0913
    request: Request, console: Console, tenant: str, principal: Principal,
    query: str, params: dict[str, Any], limit: int | None, saved: str | None = None,
) -> CypherResult:
    audit_this(request)
    try:
        result = await console.run(
            tenant, query, params=params, limit=limit, consumer=principal.consumer,
            gone=request.is_disconnected,
        )
    except ConsoleError as exc:
        log.info(
            "cypher.refused",
            extra={"tenant": tenant, "consumer": principal.consumer, "status": exc.status,
                   "saved": saved, "query": query[:2000]},
        )
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    log.info(
        "cypher.run",
        extra={"tenant": tenant, "consumer": principal.consumer, "saved": saved,
               "rows": len(result["rows"]), "elapsed_ms": result["elapsed_ms"],
               "query": query[:2000]},
    )
    return CypherResult.model_validate(result)


@router.post(
    "/{tenant}/cypher",
    summary="Run a read-only Cypher query on the tenant's graph",
    description=(
        "Off unless the server runs with `GRAPHOS_CYPHER=true` (404); needs "
        "`GRAPHOS_CYPHER_LEVEL` on the tenant (`read` by default). The query runs in a "
        "read-only transaction on the tenant's graph, with `params` as `$name`. Only "
        "Cypher's own functions; no variable-length patterns (`-[*1..3]->`) or "
        "`vertex_stats()`. At most `limit` rows, capped by `GRAPHOS_CYPHER_MAX_ROWS` (5000 "
        "by default); `truncated` says more matched. Stopped after `GRAPHOS_CYPHER_TIMEOUT_S` "
        "(30 s by default). One query at a time per user. Each run is written to the audit "
        "log with its text."
    ),
    response_model=CypherResult,
    responses=_ERRORS,
)
async def run_query(
    tenant: Tenant, body: CypherRequest, request: Request, principal: ReadPrincipal,
) -> CypherResult:
    console = _console(request, principal, tenant)
    return await _run(request, console, tenant, principal, body.query, body.params, body.limit)


# ── Saved queries ──────────────────────────────────────────────────────────


def _saved(row: Any) -> SavedQuery:
    definition = json.loads(row["definition"])
    return SavedQuery(
        name=row["name"], created_by=row["created_by"], updated_at=row["updated_at"],
        **definition,
    )


@router.get(
    "/{tenant}/cypher/saved",
    summary="The tenant's saved Cypher queries",
    description=(
        "By name: each one's text, description, parameters and how to show its result, and "
        "who saved it last. Needs what running a query needs; 404 while the console is off."
    ),
    responses={404: _ERRORS[404]},
)
async def list_saved(
    tenant: Tenant, request: Request, principal: ReadPrincipal,
) -> list[SavedQuery]:
    _console(request, principal, tenant)
    rows = await request.app.state.pool.fetch(
        "SELECT name, definition::text AS definition, created_by, updated_at "
        "FROM graphos_meta.saved_queries WHERE tenant = $1 ORDER BY name;",
        tenant,
    )
    return [_saved(r) for r in rows]


@router.get(
    "/{tenant}/cypher/saved/{name}",
    summary="One saved Cypher query",
    description=(
        "Its text, description, parameters and how to show its result, and who saved it "
        "last. Needs what running a query needs; 404 while the console is off, or when there "
        "is no such query."
    ),
    responses={404: {"description": "The console is off, or no saved query has this name."}},
)
async def get_saved(
    tenant: Tenant, name: SavedName, request: Request, principal: ReadPrincipal,
) -> SavedQuery:
    _console(request, principal, tenant)
    row = await request.app.state.pool.fetchrow(
        "SELECT name, definition::text AS definition, created_by, updated_at "
        "FROM graphos_meta.saved_queries WHERE tenant = $1 AND name = $2;",
        tenant, name,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no saved query {name!r}",
        )
    return _saved(row)


@router.put(
    "/{tenant}/cypher/saved/{name}",
    summary="Save a Cypher query under a name (replaces one of that name)",
    description=(
        "`maintain` on the tenant, and the console on (404). The query is not run or checked "
        "until it is run. Replacing a saved query makes the caller its `created_by`."
    ),
    responses={404: _ERRORS[404]},
)
async def save(
    tenant: Tenant,
    body: SavedQueryBody,
    request: Request,
    principal: MaintainPrincipal,
    name: SavedName,
) -> SavedQuery:
    _console(request, principal, tenant)
    row = await request.app.state.pool.fetchrow(
        """
        INSERT INTO graphos_meta.saved_queries (tenant, name, definition, created_by)
        VALUES ($1, $2, $3::jsonb, $4)
        ON CONFLICT (tenant, name) DO UPDATE
        SET definition = EXCLUDED.definition, created_by = EXCLUDED.created_by, updated_at = now()
        RETURNING name, definition::text AS definition, created_by, updated_at;
        """,
        tenant, name, body.model_dump_json(), principal.consumer,
    )
    return _saved(row)


@router.delete(
    "/{tenant}/cypher/saved/{name}",
    summary="Delete a saved Cypher query",
    description="`maintain` on the tenant, and the console on (404).",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: _ERRORS[404]},
)
async def delete_saved(
    tenant: Tenant,
    request: Request,
    principal: MaintainPrincipal,
    name: SavedName,
) -> None:
    _console(request, principal, tenant)
    done = await request.app.state.pool.execute(
        "DELETE FROM graphos_meta.saved_queries WHERE tenant = $1 AND name = $2;", tenant, name,
    )
    if done == "DELETE 0":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no saved query {name!r}",
        )


_TYPES = {"string": (str,), "number": (int, float), "boolean": (bool,), "list": (list,)}


@router.post(
    "/{tenant}/cypher/saved/{name}:run",
    summary="Run a saved Cypher query with its parameters",
    description=(
        "Every parameter the saved query declares must be sent in `params`, with a value of "
        "its type (`string`, `number`, `boolean`, `list`), and no other (422). Otherwise as "
        "`POST /v1/{tenant}/cypher`: the same access, limits and audit."
    ),
    response_model=CypherResult,
    responses=_ERRORS,
)
async def run_saved(
    tenant: Tenant,
    body: SavedQueryRun,
    request: Request,
    principal: ReadPrincipal,
    name: SavedName,
) -> CypherResult:
    console = _console(request, principal, tenant)
    row = await request.app.state.pool.fetchrow(
        "SELECT name, definition::text AS definition, created_by, updated_at "
        "FROM graphos_meta.saved_queries WHERE tenant = $1 AND name = $2;",
        tenant, name,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no saved query {name!r}",
        )
    saved = _saved(row)
    for param, spec in saved.params.items():
        if param not in body.params:
            raise HTTPException(status_code=422, detail=f"parameter {param!r} is missing")
        value = body.params[param]
        bad = isinstance(value, bool) and spec.type == "number"
        if bad or not isinstance(value, _TYPES[spec.type]):
            raise HTTPException(status_code=422, detail=f"parameter {param!r} takes a {spec.type}")
    unknown = sorted(set(body.params) - set(saved.params))
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown parameters: {', '.join(unknown)}")
    return await _run(
        request, console, tenant, principal, saved.query, body.params, body.limit, name,
    )

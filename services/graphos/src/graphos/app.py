"""FastAPI app factory + lifespan hook.

Lifespan responsibilities:
- Build the asyncpg pool.
- Load the auth token store (idempotent SIGHUP reload registered too).
- Start the background job runner (stopped before the pool closes).
- Mark the service ready.

graphos does **not** bootstrap any tenants on startup — consumers register
their own context via ``POST /v1/tenants`` (see bootstrap.py).
"""

from __future__ import annotations

import logging
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from graphos import __version__
from graphos.api import (
    bulk,
    cache,
    changes,
    communities,
    db,
    graph,
    health,
    info,
    jobs,
    louvain,
    me,
    paths,
    snapshot,
    tenants,
)
from graphos.auth import TokenStore
from graphos.bootstrap import (
    bootstrap_tenant,
    load_registered_tenants,
    registered_tenants,
)
from graphos.changes import ChangeFeed
from graphos.config import Settings
from graphos.contracts.db import VacuumRequest
from graphos.contracts.louvain import RecomputeCommunitiesRequest
from graphos.contracts.tenants import ResolveEdgesRequest
from graphos.db import create_pool, ensure_internal_schema
from graphos.jobs import JobKind, JobRunner
from graphos.louvain_compute import terminate_running
from graphos.observability.audit import AuditMiddleware
from graphos.observability.logging import RequestContextMiddleware, configure_logging
from graphos.observability.metrics import MetricsMiddleware, metrics_handler
from graphos.observability.rate_limit import RateLimitMiddleware
from graphos.sync import CACHE_CHANNEL, Listener, RegistrySync

log = logging.getLogger(__name__)


def _install_sighup_handler(token_store: TokenStore) -> None:
    """Reload auth tokens on SIGHUP (Unix only; no-op elsewhere)."""
    try:
        signal.signal(signal.SIGHUP, lambda *_: token_store.reload())  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        # Windows lacks SIGHUP; or we're not on the main thread.
        log.info("SIGHUP handler not installed (likely Windows)")


def _registered(tenant: str | None) -> str:
    """A queued job's tenant may have been unregistered since."""
    if tenant is None or tenant not in registered_tenants():
        raise HTTPException(status_code=404, detail=f"tenant '{tenant}' is not registered")
    return tenant


async def _run_recompute(pool: asyncpg.Pool, tenant: str | None, params: dict) -> BaseModel:
    body = RecomputeCommunitiesRequest.model_validate(params)
    return await louvain.run_recompute(pool, _registered(tenant), body)


async def _run_resolve(pool: asyncpg.Pool, tenant: str | None, params: dict) -> BaseModel:
    body = ResolveEdgesRequest.model_validate(params)
    return await graph.run_resolve(pool, _registered(tenant), body)


async def _run_vacuum(pool: asyncpg.Pool, _tenant: str | None, params: dict) -> BaseModel:
    return await db.run_vacuum(pool, VacuumRequest.model_validate(params))


# Operations that accept ``Prefer: respond-async``.
JOB_KINDS: dict[str, JobKind] = {
    "recompute_communities": JobKind(run=_run_recompute, admin_only=True),
    "resolve_edges": JobKind(run=_run_resolve),
    "vacuum": JobKind(run=_run_vacuum, admin_only=True),
}


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    # 1. Token store.  Fail closed: without a tokens file graphos only
    # starts when auth was disabled explicitly (local development).
    if settings.auth_tokens_file is None:
        if not settings.auth_disabled:
            raise RuntimeError(
                "No GRAPHOS_AUTH_TOKENS_FILE configured. Point it at a tokens "
                "file (scripts/init-secrets.sh creates one), or set "
                "GRAPHOS_AUTH_DISABLED=true for local development only."
            )
        app.state.token_store = None
        log.error(
            "AUTHENTICATION DISABLED (GRAPHOS_AUTH_DISABLED=true): every request "
            "is an anonymous admin. Never expose this instance to a network."
        )
    else:
        token_store = TokenStore(settings.auth_tokens_file)
        if token_store.load_error is not None:
            raise RuntimeError(
                f"GRAPHOS_AUTH_TOKENS_FILE {settings.auth_tokens_file} is unusable "
                f"({token_store.load_error}). Fix or create it (scripts/init-secrets.sh)."
            )
        app.state.token_store = token_store
        _install_sighup_handler(token_store)

    # 2. asyncpg pool
    pool = await create_pool(settings)
    app.state.pool = pool

    # 3. graphos-internal schema (idempotent) + restore registered tenants
    #    from the previous boot.
    await ensure_internal_schema(pool)
    restored = await load_registered_tenants(pool)

    # Re-bootstrap each restored tenant in case AGE was wiped underneath
    # graphos-api (e.g. the operator dropped a graph). Each call is idempotent.
    # A tenant whose bootstrap fails (e.g. existing rows violate a unique
    # index) is logged and skipped: one bad tenant must not keep the
    # service, and every other tenant, from starting.
    for spec in list(registered_tenants().values()):
        try:
            await bootstrap_tenant(
                pool, spec, vertex_graphid_indexes=settings.vertex_graphid_indexes,
            )
        except Exception:  # noqa: BLE001
            log.exception("tenant bootstrap failed at startup", extra={"tenant": spec.name})

    # 4. Background jobs.
    runner = JobRunner(
        pool, JOB_KINDS,
        concurrency=settings.job_concurrency,
        poll_s=settings.job_poll_s,
        heartbeat_s=settings.job_heartbeat_s,
        retention_h=settings.job_retention_h,
    )
    runner.start()
    app.state.job_runner = runner

    # 5. One LISTEN connection for this process: change-feed wake-ups, and
    #    keeping the tenant registry and the cache LRU in step with the other
    #    replicas (graphos.sync).
    listener = Listener(pool)
    feed = ChangeFeed(
        pool, listener,
        retention_h=settings.changes_retention_h, poll_s=settings.changes_poll_s,
    )
    registry_sync = RegistrySync(pool, listener, interval_s=settings.registry_sync_s)
    cache.LRU.max_age_s = settings.cache_max_age_s
    listener.subscribe(CACHE_CHANNEL, cache.on_notification)
    listener.on_connect(cache.LRU.clear)
    listener.start()
    feed.start()
    registry_sync.start()
    app.state.change_feed = feed
    app.state.registry_sync = registry_sync
    app.state.listener = listener

    log.info(
        "graphos ready",
        extra={"version": __version__, "tenants_restored": restored},
    )

    try:
        yield
    finally:
        await registry_sync.stop()
        await feed.stop()
        await listener.stop()
        await runner.stop()
        terminate_running()
        await pool.close()
        log.info("graphos shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """FastAPI app factory used by uvicorn (``--factory``)."""
    settings = settings or Settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    app = FastAPI(
        title="graphos",
        version=__version__,
        description=(
            "Multi-tenant graph service backed by PostgreSQL + Apache AGE. "
            "Consumers speak typed HTTP; graphos owns the SQL."
        ),
        lifespan=_lifespan,
    )
    app.state.settings = settings

    async def _statement_timeout(request: Request, exc: Exception) -> JSONResponse:
        # Postgres cancelled the statement at ``statement_timeout`` (or the
        # client-side backstop fired).  RFC 7807 body so callers can tell a
        # slow query apart from a bad request.
        log.warning("statement timeout path=%s err=%s", request.url.path, type(exc).__name__)
        return JSONResponse(
            status_code=504,
            media_type="application/problem+json",
            content={
                "type": "about:blank",
                "title": "Query timed out",
                "status": 504,
                "detail": (
                    "The database statement exceeded the server's statement timeout "
                    f"({settings.statement_timeout_s:g}s). Narrow the request "
                    "(lower limit, fewer hops, a label or property filter) and retry."
                ),
            },
        )

    app.add_exception_handler(asyncpg.exceptions.QueryCanceledError, _statement_timeout)
    app.add_exception_handler(TimeoutError, _statement_timeout)

    # Observability middleware stack. Outermost runs first on the
    # request path, last on response — FastAPI applies them in
    # reverse-registration order. Registration order here gives the
    # actual on-the-wire execution:
    #
    #     RequestContext  ← outermost on request (binds trace_id first
    #                        so audit log etc. can use it)
    #     RateLimit       ← rejects before any work happens
    #     Metrics         ← times only requests that aren't rate-limited
    #     Audit           ← innermost: sees the final status code
    app.add_middleware(
        AuditMiddleware,
        enabled=settings.audit_enabled,
        writes_only=settings.audit_endpoints_only_writes,
    )
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(
        RateLimitMiddleware,
        enabled=settings.rate_limit_enabled,
        rps=settings.rate_limit_rps,
        window_s=settings.rate_limit_window_s,
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_route("/metrics", metrics_handler, methods=["GET"])

    # Routers (no prefix on health; /v1 prefix on every other surface)
    app.include_router(health.router)
    app.include_router(info.router, prefix="/v1")
    app.include_router(tenants.router, prefix="/v1")
    app.include_router(me.router, prefix="/v1")
    app.include_router(graph.router, prefix="/v1")
    app.include_router(paths.router, prefix="/v1")
    app.include_router(bulk.router, prefix="/v1")
    app.include_router(db.router, prefix="/v1")
    app.include_router(louvain.router, prefix="/v1")
    app.include_router(communities.router, prefix="/v1")
    app.include_router(cache.router, prefix="/v1")
    app.include_router(snapshot.router, prefix="/v1")
    app.include_router(jobs.router, prefix="/v1")
    app.include_router(changes.router, prefix="/v1")

    @app.get("/")
    async def _root() -> dict[str, Any]:
        return {
            "service": "graphos",
            "version": __version__,
            "docs": "/docs",
            "openapi": "/openapi.json",
        }

    return app

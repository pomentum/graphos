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

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from graphos import __version__, api_docs, console, identity, node_properties, oidc, properties
from graphos.api import (
    access,
    bulk,
    cache,
    changes,
    communities,
    db,
    graph,
    graph_map,
    graph_metrics,
    health,
    info,
    jobs,
    louvain,
    me,
    paths,
    property_check,
    snapshot,
    tenants,
)
from graphos.api import auth as auth_api
from graphos.api import cypher as cypher_api
from graphos.auth import TokenAuthenticator
from graphos.bootstrap import (
    bootstrap_tenant,
    load_registered_tenants,
    registered_tenants,
)
from graphos.changes import ChangeFeed
from graphos.config import Settings
from graphos.contracts.db import VacuumRequest
from graphos.contracts.graph_metrics import ComputeMetricsRequest
from graphos.contracts.louvain import RecomputeCommunitiesRequest
from graphos.contracts.property_schema import PropertiesCheckRequest
from graphos.contracts.tenants import ResolveEdgesRequest
from graphos.db import create_pool, ensure_internal_schema
from graphos.graph_compute import terminate_running
from graphos.graph_metrics import configure as configure_metrics
from graphos.jobs import JobKind, JobRunner
from graphos.observability.audit import AuditMiddleware
from graphos.observability.logging import RequestContextMiddleware, configure_logging
from graphos.observability.metrics import MetricsMiddleware, metrics_handler
from graphos.observability.rate_limit import RateLimitMiddleware
from graphos.sync import CACHE_CHANNEL, Listener, RegistrySync

log = logging.getLogger(__name__)


def _registered(tenant: str | None) -> str:
    """A queued job's tenant may have been unregistered since."""
    if tenant is None or tenant not in registered_tenants():
        raise HTTPException(status_code=404, detail=f"tenant '{tenant}' is not registered")
    return tenant


async def _run_recompute(pool: asyncpg.Pool, tenant: str | None, params: dict) -> BaseModel:
    body = RecomputeCommunitiesRequest.model_validate(params)
    return await louvain.run_recompute(pool, _registered(tenant), body)


async def _run_metrics(pool: asyncpg.Pool, tenant: str | None, params: dict) -> BaseModel:
    body = ComputeMetricsRequest.model_validate(params)
    return await graph_metrics.run_compute(pool, _registered(tenant), body)


async def _run_resolve(pool: asyncpg.Pool, tenant: str | None, params: dict) -> BaseModel:
    body = ResolveEdgesRequest.model_validate(params)
    return await graph.run_resolve(pool, _registered(tenant), body)


async def _run_check(pool: asyncpg.Pool, tenant: str | None, params: dict) -> BaseModel:
    body = PropertiesCheckRequest.model_validate(params)
    return await property_check.run_check(pool, _registered(tenant), body)


async def _run_vacuum(pool: asyncpg.Pool, _tenant: str | None, params: dict) -> BaseModel:
    return await db.run_vacuum(pool, VacuumRequest.model_validate(params))


# Operations that accept ``Prefer: respond-async``.
JOB_KINDS: dict[str, JobKind] = {
    "recompute_communities": JobKind(run=_run_recompute, needs="maintain"),
    "compute_metrics": JobKind(run=_run_metrics, needs="maintain"),
    "resolve_edges": JobKind(run=_run_resolve),
    "check_properties": JobKind(run=_run_check, needs="maintain"),
    "vacuum": JobKind(run=_run_vacuum, needs="admin"),
}


async def setup_identity(
    app: FastAPI, pool: asyncpg.Pool, settings: Settings,
    oidc_transport: httpx.AsyncBaseTransport | None = None,
) -> identity.AccessStore | None:
    """Users and grants in memory, the signing key, the authenticator, and
    the bootstrap admin on a first start.  None when auth is disabled."""
    app.state.authenticator = None
    app.state.access_store = None
    app.state.oidc = None
    if settings.auth_disabled:
        return None
    provider = oidc.provider_from(settings, transport=oidc_transport)
    if provider is not None:
        try:
            await provider.metadata()
        except oidc.OidcError as exc:
            # Not fatal: graphos users still sign in; SSO works once it answers.
            log.warning("single sign-on: %s", exc)
    app.state.oidc = provider
    store = identity.AccessStore(pool)
    created = await store.bootstrap(settings.admin_password_file)
    await store.load()
    if created is not None:
        log.warning(
            "FIRST START: created the admin user '%s' with %s; it must be changed at the "
            "first sign-in", identity.ADMIN_USER,
            f"the password '{identity.DEFAULT_ADMIN_PASSWORD}'" if created == "default"
            else "the password from GRAPHOS_ADMIN_PASSWORD_FILE",
        )
    if store.first_password_unchanged():
        log.warning(
            "the admin user '%s' still has its first password: sign in and change it",
            identity.ADMIN_USER,
        )
    signer = identity.Signer(issuer=settings.auth_issuer, ttl_s=settings.access_token_ttl_s)
    await signer.load(pool, settings.auth_signing_key_file)
    app.state.access_store = store
    app.state.signer = signer
    app.state.authenticator = TokenAuthenticator(signer, store, provider)
    app.state.login_throttle = identity.Throttle(settings.login_attempts_per_min)
    return store


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    # 1. Say it loudly when nothing is checked.
    if settings.auth_disabled:
        log.error(
            "AUTHENTICATION DISABLED (GRAPHOS_AUTH_DISABLED=true): every request "
            "is an anonymous admin. Never expose this instance to a network."
        )

    # 2. asyncpg pool
    pool = await create_pool(settings)
    app.state.pool = pool

    # 3. graphos-internal schema (idempotent) + restore registered tenants
    #    from the previous boot.
    await ensure_internal_schema(pool)
    restored = await load_registered_tenants(pool)

    # 3a. Identity: users and grants in memory, the signing key, and the
    #     bootstrap admin on a first start (graphos.identity).
    access_store = await setup_identity(app, pool, settings)

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

    # 3b. Tenants whose graph predates graphos_meta.node_properties: record
    #     their property names once, in the background (reads every node).
    property_backfill = asyncio.create_task(
        node_properties.backfill_incomplete(pool, list(registered_tenants())),
        name="graphos-node-properties-backfill",
    )

    # 3c. The Cypher console, when turned on (graphos.console): a small pool
    #     of its own, so console queries never take the API's connections.
    app.state.console = await console.start(settings, pool)

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
    if access_store is not None:
        listener.subscribe(identity.ACCESS_CHANNEL, access_store.on_notify)
        listener.on_connect(access_store.on_notify)
        access_store.start()
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
        property_backfill.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await property_backfill
        await registry_sync.stop()
        await feed.stop()
        await listener.stop()
        if access_store is not None:
            await access_store.stop()
        await runner.stop()
        terminate_running()
        if app.state.console is not None:
            await app.state.console.pool.close()
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
        # Served from graphos's own files, not a CDN (graphos.api_docs).
        docs_url=None,
        redoc_url=None,
    )
    api_docs.install(app)
    app.state.settings = settings
    configure_metrics(betweenness_max_nodes=settings.betweenness_max_nodes)

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

    async def _unique_violation(request: Request, exc: Exception) -> JSONResponse:
        # A write that would give two nodes the values of a unique property
        # index (graphos.declared).  The constraint name is the index name.
        index = getattr(exc, "constraint_name", None) or "a unique index"
        log.info("unique violation path=%s index=%s", request.url.path, index)
        return JSONResponse(
            status_code=409,
            media_type="application/problem+json",
            content={
                "type": "about:blank",
                "title": "Unique property values taken",
                "status": 409,
                "detail": (
                    f"The write would give two nodes the same values of the unique index "
                    f"{index}. Rows of the request flushed before it stay written."
                ),
            },
        )

    app.add_exception_handler(asyncpg.exceptions.UniqueViolationError, _unique_violation)

    async def _property_refusal(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, properties.Refused)
        log.info("property schema refusal path=%s total=%d", request.url.path, exc.total)
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "about:blank",
                "title": "Property schema violations",
                "status": 422,
                "detail": (
                    f"{exc.total} property violations (property_schema, mode strict); "
                    "nothing was written"
                ),
                "total": exc.total,
                "violations": exc.listed,
            },
        )

    app.add_exception_handler(properties.Refused, _property_refusal)

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
    app.include_router(auth_api.router, prefix="/v1")
    app.include_router(access.router, prefix="/v1")
    app.include_router(tenants.router, prefix="/v1")
    app.include_router(me.router, prefix="/v1")
    app.include_router(graph.router, prefix="/v1")
    app.include_router(paths.router, prefix="/v1")
    app.include_router(graph_map.router, prefix="/v1")
    app.include_router(bulk.router, prefix="/v1")
    app.include_router(db.router, prefix="/v1")
    app.include_router(louvain.router, prefix="/v1")
    app.include_router(graph_metrics.router, prefix="/v1")
    app.include_router(property_check.router, prefix="/v1")
    app.include_router(communities.router, prefix="/v1")
    app.include_router(cache.router, prefix="/v1")
    app.include_router(snapshot.router, prefix="/v1")
    app.include_router(jobs.router, prefix="/v1")
    app.include_router(changes.router, prefix="/v1")
    app.include_router(cypher_api.router, prefix="/v1")

    @app.get("/")
    async def _root() -> dict[str, Any]:
        return {
            "service": "graphos",
            "version": __version__,
            "docs": "/docs",
            "redoc": "/redoc",
            "openapi": "/openapi.json",
        }

    return app

"""Prometheus metrics — registry, instruments, ASGI middleware.

The middleware records:

- ``graphos_requests_total{method, route, status}``      — request counter
- ``graphos_request_duration_seconds{method, route}``    — latency histogram
- ``graphos_db_pool_size{state}``                        — asyncpg pool gauge

Route labels use the FastAPI route's *path template* (e.g.
``/v1/{tenant}/graph/search``), not the concrete URL — so two requests
that hit the same handler share a bucket even though their tenant
labels differ. This is the textbook way to keep label cardinality
bounded.

The ``/metrics`` endpoint is mounted by the app factory and uses the
default global Prometheus registry.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request, Response
from fastapi.responses import PlainTextResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware

from graphos.bootstrap import registered_tenants

log = logging.getLogger(__name__)


# ── Instruments ────────────────────────────────────────────────────────────


REQUESTS_TOTAL = Counter(
    "graphos_requests_total",
    "Number of HTTP requests processed.",
    labelnames=("method", "route", "status"),
)

PROPERTY_VIOLATIONS = Counter(
    "graphos_property_violations_total",
    "Property-schema problems in written rows: refused (strict) or warned.",
    # No tenant label: /metrics needs no sign-in, and tenant names are not
    # for anyone who can reach the port.  Per-tenant detail is in the write
    # responses (property_warnings, property_violations).
    labelnames=("kind", "outcome", "rule"),
)

REQUEST_DURATION = Histogram(
    "graphos_request_duration_seconds",
    "Request latency in seconds, partitioned by route.",
    labelnames=("method", "route"),
    # Buckets sized for graphos's expected p50 (~ms) → p999 (~10s for bulk).
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

DB_POOL_SIZE = Gauge(
    "graphos_db_pool_size",
    "Connection pool stats reported by asyncpg.",
    labelnames=("state",),  # active / idle / total
)

REGISTERED_TENANTS = Gauge(
    "graphos_registered_tenants",
    "Number of tenants currently in the in-process registry.",
)


# ── Middleware ──────────────────────────────────────────────────────────────


def _route_label(request: Request) -> str:
    """Return the route's path template, or '<unmatched>' if no route fired.

    Using the template keeps label cardinality bounded: every tenant or
    node_id hits ``/v1/{tenant}/graph/neighbors`` (one bucket) rather
    than a unique URL per call.
    """
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path"):
        return route.path
    return "<unmatched>"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # ``/metrics`` is itself exposed via this middleware — we want
        # counters incrementing for everything except literally hitting
        # the scrape endpoint (which would cause unbounded self-traffic
        # in the Counter). It's a small policy choice; the alternative
        # is to keep /metrics itself instrumented and accept the bias.
        if request.url.path == "/metrics":
            return await call_next(request)

        started = time.perf_counter()
        response: Response | None = None
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            status_code = 500
            raise
        finally:
            elapsed = time.perf_counter() - started
            route = _route_label(request)
            REQUESTS_TOTAL.labels(
                method=request.method,
                route=route,
                status=str(status_code),
            ).inc()
            REQUEST_DURATION.labels(
                method=request.method,
                route=route,
            ).observe(elapsed)


# ── /metrics handler ───────────────────────────────────────────────────────


async def metrics_handler(request: Request) -> Response:
    """GET /metrics — Prometheus exposition format.

    Refreshes the pool-size gauge on each scrape so the dashboard always
    sees fresh numbers. Not authenticated by default; if exposed beyond
    the internal network, gate it at the reverse proxy.
    """
    pool = getattr(request.app.state, "pool", None)
    if pool is not None:
        try:
            DB_POOL_SIZE.labels(state="total").set(pool.get_size())
            DB_POOL_SIZE.labels(state="idle").set(pool.get_idle_size())
            DB_POOL_SIZE.labels(state="active").set(
                pool.get_size() - pool.get_idle_size(),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read pool stats", extra={"error": str(exc)})

    # Tenants gauge is updated only here (instead of by the bootstrap
    # routines) so we don't introduce a coupling from bootstrap/registry
    # code to metrics. Cheap O(1) read of an in-process dict.
    REGISTERED_TENANTS.set(len(registered_tenants()))

    return PlainTextResponse(
        generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


def record_property_tally(kind: str, tally: Any) -> None:
    """Count a request's property problems (``graphos.properties.Tally``) by rule,
    not by property, to keep the label set small."""
    for outcome, counts in (("refused", tally.violations), ("warned", tally.warnings)):
        for key, n in counts.items():
            rule = key.rsplit(":", 1)[-1]
            PROPERTY_VIOLATIONS.labels(kind=kind, outcome=outcome, rule=rule).inc(n)

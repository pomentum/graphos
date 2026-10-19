"""structlog setup + traceparent-aware request context.

Two pieces:

1. ``configure_logging(settings)`` wires structlog onto stdlib logging
   with a JSON renderer (default) or a console renderer (for dev).
   Adds a contextvar-based binding so per-request context (trace_id,
   tenant, consumer) is included on every log line emitted during the
   request — no need to manually pass loggers around.

2. ``RequestContextMiddleware`` reads the inbound ``traceparent``
   header (W3C Trace Context), parses out the trace_id, binds it +
   the route into the contextvar for the duration of the request.
   This is propagation only — we don't try to be a full OpenTelemetry
   exporter. Downstream services that need spans wire OTel separately;
   we just pass trace_id through so log lines from one request are
   correlatable across services.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# W3C traceparent: version "-" trace-id "-" parent-id "-" trace-flags
# Spec: https://www.w3.org/TR/trace-context/#traceparent-header
_TRACEPARENT_RE = re.compile(
    r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Install structlog's processors over stdlib logging.

    Idempotent: re-calling reconfigures cleanly.
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
    ]

    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # ``ExtraAdder`` copies a stdlib record's ``extra={...}`` fields into
        # the event dict.  Without it the audit log (stdlib ``logging`` with
        # ``extra``) renders as a bare "write" line with no consumer, tenant
        # or status.
        foreign_pre_chain=[*shared_processors, structlog.stdlib.ExtraAdder()],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    # Mark our handler so re-calls to configure_logging() can replace
    # only THIS handler — not pytest's caplog, not any other handler the
    # embedding application has installed.
    handler.set_name("graphos_structured")

    root = logging.getLogger()
    root.handlers = [
        h for h in root.handlers if getattr(h, "name", None) != "graphos_structured"
    ]
    root.addHandler(handler)
    root.setLevel(level.upper())
    # httpx logs every request at INFO; graphos's own requests (MCP tools run their routes
    # in process, the identity provider) are logged where they matter.
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))


def _parse_traceparent(value: str) -> str | None:
    """Return the 32-hex trace_id from a traceparent header, or None."""
    m = _TRACEPARENT_RE.match(value.strip())
    if not m:
        return None
    trace_id = m.group(2)
    # All-zeros trace_id is reserved as "invalid" by the spec.
    if trace_id == "0" * 32:
        return None
    return trace_id


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path"):
        return route.path
    return request.url.path


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind per-request fields (trace_id, route, method) into structlog's
    contextvars so every log line emitted during the request carries them.

    On exit, the previous bindings are restored — structlog's
    ``bound_contextvars`` is a real context manager.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        traceparent = request.headers.get("traceparent", "")
        trace_id = _parse_traceparent(traceparent) if traceparent else None
        bindings = {
            "method": request.method,
            "route": _route_label(request),
        }
        if trace_id:
            bindings["trace_id"] = trace_id

        with structlog.contextvars.bound_contextvars(**bindings):
            response = await call_next(request)
            # Mirror the trace_id back so callers can correlate without
            # round-tripping the header themselves.
            if trace_id:
                response.headers["traceparent"] = traceparent
            return response

"""Write audit log middleware.

Emits one structured log line per state-changing request. Reads:

  - method, path, route template (from request scope)
  - tenant (when the path includes ``/v1/{tenant}/...``)
  - consumer (token principal — bound by auth, but the middleware
    reads it from the request scope without re-validating the token)
  - args_hash: sha256 of the request body, truncated to 16 hex chars.
    Bounded log size; lets you correlate "same payload" without
    storing the payload itself.
  - status_code + elapsed_ms

The middleware is layered AFTER the auth dependency runs, so it can
read ``request.scope["principal"]`` when the route depended on
``require_principal``. We don't repeat the auth check; if the route
didn't require auth, ``consumer`` is logged as "anonymous".

Switched off entirely when ``settings.audit_enabled = false``.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Number of path segments required before a tenant name appears:
# ``/v1/<tenant>/...`` → after lstrip+split → ["v1", "<tenant>", ...]
_MIN_PARTS_FOR_TENANT = 2

# stdlib logger is lazy (looked up by name on every call) so the
# structlog processor chain installed by configure_logging() catches
# it regardless of import order.
log = logging.getLogger("graphos.audit")


def _hash_body(body: bytes) -> str:
    if not body:
        return ""
    return hashlib.sha256(body).hexdigest()[:16]


def _extract_tenant_from_path(path: str) -> str | None:
    """If path is ``/v1/<tenant>/...``, return ``<tenant>``. Else None."""
    parts = path.lstrip("/").split("/", 2)
    if len(parts) >= _MIN_PARTS_FOR_TENANT and parts[0] == "v1":
        # Some non-tenant routes also start with /v1 (e.g. /v1/info,
        # /v1/tenants, /v1/db/...). Treat anything in the
        # "graphos-reserved" set as tenant-less.
        candidate = parts[1]
        if candidate in {"info", "tenants", "db", "admin"}:
            return None
        return candidate
    return None


class AuditMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, enabled: bool = True, writes_only: bool = True):  # noqa: ANN001
        super().__init__(app)
        self._enabled = enabled
        self._writes_only = writes_only

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not self._enabled:
            return await call_next(request)
        if self._writes_only and request.method not in _WRITE_METHODS:
            return await call_next(request)

        # Buffer the body once so the downstream handler can re-read it.
        # FastAPI buffers JSON bodies in memory anyway; this is the same
        # cost in a different place.
        body = await request.body()
        args_hash = _hash_body(body)

        async def _receive() -> dict:
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = _receive   # type: ignore[assignment]  # starlette private API

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # A write that crashed is the one an audit trail most needs:
            # record it as a 500 before the error propagates.
            _emit(request, args_hash, _INTERNAL_ERROR, started)
            raise
        _emit(request, args_hash, response.status_code, started)
        return response


_INTERNAL_ERROR = 500


def _emit(request: Request, args_hash: str, status: int, started: float) -> None:
    consumer = "anonymous"
    principal = request.scope.get("principal")
    if principal is not None and hasattr(principal, "consumer"):
        consumer = principal.consumer

    # stdlib logging takes a message + extra; structlog's processor
    # chain picks `extra` up as fields and renders the JSON line.
    log.info(
        "write",
        extra={
            "method": request.method,
            "path": request.url.path,
            "tenant": _extract_tenant_from_path(request.url.path),
            "consumer": consumer,
            "args_hash": args_hash,
            "status": status,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        },
    )

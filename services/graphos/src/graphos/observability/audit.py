"""Write audit log middleware.

Emits one structured log line per state-changing request: one whose route
declares ``write`` access (``graphos.auth``), whatever its HTTP method, so a
read sent as ``POST`` (``nodes:scan``, ``edges/aggregate``) is not logged.
A request that reached no route's auth falls back to its method.  A read
that hands out a lot at once (a dataset export, a Cypher console query)
asks to be logged too, with :func:`audit_this` (message ``read``).  With
``data_reads`` (``GRAPHOS_AUDIT_READS=data``) every read of a tenant's data
is logged as well (any route under ``/v1/{tenant}/`` that needs ``read``),
with its query string, so the trail says who looked at what.  Reads:

  - method, path, route template (from request scope)
  - tenant (when the path includes ``/v1/{tenant}/...``)
  - consumer (token principal — bound by auth, but the middleware
    reads it from the request scope without re-validating the token)
  - query: the query string, on ``read`` lines (which node, which search)
  - args_hash: sha256 of the request body, truncated to 16 hex chars.
    Bounded log size; lets you correlate "same payload" without
    storing the payload itself.  Hashed chunk by chunk as the endpoint
    reads the body, so a streamed upload (``nodes:bulk``,
    ``snapshot-import``) is never held in memory here; it covers the
    bytes the endpoint read.
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

from graphos.auth import ACCESS_SCOPE_KEY, Access

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Set in a request's scope by :func:`audit_this`.
AUDIT_READ_KEY = "graphos.audit_read"

# Number of path segments required before a tenant name appears:
# ``/v1/<tenant>/...`` → after lstrip+split → ["v1", "<tenant>", ...]
_MIN_PARTS_FOR_TENANT = 2

# stdlib logger is lazy (looked up by name on every call) so the
# structlog processor chain installed by configure_logging() catches
# it regardless of import order.
log = logging.getLogger("graphos.audit")


def audit_this(request: Request) -> None:
    """Log this request although it is a read (a whole dataset export)."""
    request.scope[AUDIT_READ_KEY] = True


# The route template of every read of one tenant's data (without the
# ``/v1`` its router is included under).
_TENANT_ROUTES = "/{tenant}/"


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
    def __init__(  # noqa: ANN204
        self, app, *, enabled: bool = True, writes_only: bool = True,  # noqa: ANN001
        data_reads: bool = False,
    ):
        super().__init__(app)
        self._enabled = enabled
        self._writes_only = writes_only
        self._data_reads = data_reads

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not self._enabled:
            return await call_next(request)

        # Hash the body as the endpoint reads it, chunk by chunk: a streamed
        # upload passes through without being held here.  Whether the
        # request is a write is only known once its route's auth has run,
        # after the call.
        digest = hashlib.sha256()
        read_any = False
        receive = request.receive

        async def _receive() -> dict:
            nonlocal read_any
            message = await receive()
            chunk = message.get("body", b"") if message.get("type") == "http.request" else b""
            if chunk:
                digest.update(chunk)
                read_any = True
            return message

        request._receive = _receive   # type: ignore[assignment]  # starlette private API

        def args_hash() -> str:
            return digest.hexdigest()[:16] if read_any else ""

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # A write that crashed is the one an audit trail most needs:
            # record it as a 500 before the error propagates.
            if self._logged(request):
                _emit(request, args_hash(), _INTERNAL_ERROR, started)
            raise
        if self._logged(request):
            _emit(request, args_hash(), response.status_code, started)
        return response

    def _logged(self, request: Request) -> bool:
        return (
            not self._writes_only
            or _is_write(request)
            or bool(request.scope.get(AUDIT_READ_KEY))
            or (self._data_reads and _is_data_read(request))
        )


_INTERNAL_ERROR = 500


def _is_write(request: Request) -> bool:
    access = request.scope.get(ACCESS_SCOPE_KEY)
    if access is None:
        return request.method in _WRITE_METHODS
    return access is not Access.READ


def _is_data_read(request: Request) -> bool:
    """A read of one tenant's data: a route under ``/v1/{tenant}/`` needing ``read``."""
    route = request.scope.get("route")
    return (
        request.scope.get(ACCESS_SCOPE_KEY) is Access.READ
        and getattr(route, "path", "").removeprefix("/v1").startswith(_TENANT_ROUTES)
    )


def _emit(request: Request, args_hash: str, status: int, started: float) -> None:
    consumer = "anonymous"
    principal = request.scope.get("principal")
    if principal is not None and hasattr(principal, "consumer"):
        consumer = principal.consumer

    # stdlib logging takes a message + extra; structlog's processor
    # chain picks `extra` up as fields and renders the JSON line.
    read = bool(request.scope.get(AUDIT_READ_KEY)) or not _is_write(request)
    log.info(
        "read" if read else "write",
        extra={
            "method": request.method,
            "path": request.url.path,
            **({"query": request.url.query} if read else {}),
            "tenant": _extract_tenant_from_path(request.url.path),
            "consumer": consumer,
            "args_hash": args_hash,
            "status": status,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        },
    )

"""Errors raised by the client."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class GraphosError(RuntimeError):
    """Wraps a non-2xx response from graphos with the status code + detail.

    ``body`` is the response's JSON body when it was one (else ``None``).
    ``code`` is its machine-readable ``code`` when the server sent one, such as
    ``"schema_managed"`` (a 409: the tenant's schema is managed by someone
    else, named in ``schema_manager``); both are ``None`` otherwise.
    """

    def __init__(
        self, status_code: int, detail: str, method: str, url: str, *, body: Any = None,
    ):
        super().__init__(f"{method} {url} → {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.method = method
        self.url = url
        self.body = body
        fields: Mapping[str, Any] = body if isinstance(body, Mapping) else {}
        code = fields.get("code")
        manager = fields.get("schema_manager")
        self.code: str | None = code if isinstance(code, str) else None
        self.schema_manager: str | None = manager if isinstance(manager, str) else None


class GraphosNotReady(GraphosError):
    """Raised on 503: graphos is starting or cannot reach its database, or the
    database is out of memory or connections (retried first, as a 5xx)."""


class SchemaDriftError(GraphosError):
    """Raised by :meth:`GraphosClient.ensure_tenant` with ``on_drift="fail"``
    when registering would replace entries that differ from the stored ones
    (``overwrites``) or create again labels, edge types or pairs removed
    earlier (``recreates``).  Nothing was registered.

    ``drift`` is the dry run's ``drift`` as the server answered it, ``lines``
    every readable line logged for it, ``blocking`` those that made it fail
    (the message lists them).  ``status_code`` is 0: the server refused
    nothing, the client stopped.

    Not to be confused with :func:`is_schema_drift`, which recognises a write
    refused because the server no longer knows a label.
    """

    def __init__(
        self, tenant: str, drift: Mapping[str, Any], lines: list[str], blocking: list[str],
    ):
        listed = "".join(f"\n- {line}" for line in blocking)
        super().__init__(
            0,
            f"{tenant}: registering would undo or bring back schema changes made "
            f"elsewhere; nothing was registered{listed}",
            "POST", "/v1/tenants",
        )
        self.tenant = tenant
        self.drift: dict[str, Any] = dict(drift)
        self.lines = list(lines)
        self.blocking = list(blocking)

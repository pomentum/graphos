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


class SchemaManagerNotApplied(GraphosError):
    """Raised after a registration that named a schema manager
    (``schema_manager=`` or ``manage_schema=True``) when the server's answer
    shows it did not apply it: the answer has no ``schema_manager`` at all (a
    server older than the schema manager, or rolled back, which ignores the
    field) or names someone else (for ``"self"``: none).

    The rest of the registration *was* applied; only the manager is not what
    was asked, so the tenant is not protected the way the caller expects.
    ``requested`` is what was sent (``None``: a release), ``answered`` what the
    server answered (``None`` when it sent none), ``result`` the whole answer.
    ``status_code`` is 0: the server refused nothing, the client stopped.
    """

    def __init__(self, tenant: str, requested: str | None, result: Mapping[str, Any]) -> None:
        answered = result.get("schema_manager")
        if "schema_manager" not in result:
            why = ("its answer has no schema_manager: the server ignores the field "
                   "(likely older than the schema manager)")
        else:
            why = f"it answered schema_manager={answered!r}"
        super().__init__(
            0,
            f"{tenant}: the server did not apply schema_manager={requested!r}; {why}. "
            "The rest of the registration was applied.",
            "POST", "/v1/tenants",
        )
        self.tenant = tenant
        self.requested = requested
        self.answered: str | None = answered if isinstance(answered, str) else None
        self.result: dict[str, Any] = dict(result)


def check_schema_manager(tenant: str, requested: str | None, result: Mapping[str, Any]) -> None:
    """Raise :class:`SchemaManagerNotApplied` unless a registration's answer
    names the manager it was sent (``"self"``: any manager)."""
    if "schema_manager" in result:
        answered = result["schema_manager"]
        if requested == "self" and answered is not None:
            return
        if requested != "self" and answered == requested:
            return
    raise SchemaManagerNotApplied(tenant, requested, result)


class SchemaDriftError(GraphosError):
    """Raised by :meth:`GraphosClient.ensure_tenant` with ``on_drift="fail"``
    when registering would replace entries that differ from the stored ones
    (``overwrites``), remove what a replace drops (``removes``), change a
    setting (``settings``) or create again labels, edge types or pairs
    removed earlier (``recreates``).  Nothing was registered.

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

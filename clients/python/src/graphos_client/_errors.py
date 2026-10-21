"""Errors raised by the client."""

from __future__ import annotations


class GraphosError(RuntimeError):
    """Wraps a non-2xx response from graphos with the status code + detail."""

    def __init__(self, status_code: int, detail: str, method: str, url: str):
        super().__init__(f"{method} {url} → {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.method = method
        self.url = url


class GraphosNotReady(GraphosError):
    """Raised on 503: graphos is starting or cannot reach its database, or the
    database is out of memory or connections (retried first, as a 5xx)."""

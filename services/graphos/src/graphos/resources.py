"""The database short of a resource (disk, memory, connections), or gone.

PostgreSQL reports a shortage as SQLSTATE class 53. graphos answers it as what
it is (507 for a full disk, 503 for the rest) instead of an internal error, so
a caller, a job's record and the web UI can tell a full disk from a bug. A
connection lost mid-statement (class 08), refused (the server down), or the
server shutting down or restarting (57P01 to 57P03) is answered 503 too: the statement did not
complete, and a retry once the database is back can. A database that cannot
write its WAL for want of disk space stops and restarts this way. The
database's own message names files and object ids: it goes to the server
log, never to the caller.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

from fastapi import HTTPException

_KNOWN: dict[str, tuple[int, str, str]] = {
    "53100": (507, "Database out of disk space",
              "The database server is out of disk space; the statement that needed more failed "
              "and was rolled back. Free space on the database server, then retry."),
    "53200": (503, "Database out of memory",
              "The database server ran out of memory; the statement failed and was rolled back. "
              "Retry later, or with a smaller request."),
    "53300": (503, "Database connections exhausted",
              "The database server has no free connection; retry shortly."),
    "53400": (503, "Database limit reached",
              "The statement hit a limit set in the database server's configuration; it failed "
              "and was rolled back."),
}
_OTHER = (503, "Database short of resources",
          "The database server is short of a resource (SQLSTATE {sqlstate}); the statement "
          "failed and was rolled back. Retry later.")
_GONE = (503, "Database unavailable",
         "The connection to the database server was lost, or the server is shutting down or "
         "restarting (SQLSTATE {sqlstate}); the statement did not complete. Retry when it is "
         "back. A server that ran out of disk space for its write-ahead log restarts this way.")
_GONE_CODES = frozenset({"57P01", "57P02", "57P03"})


@dataclass(frozen=True)
class Shortage:
    """What the database ran short of, as graphos answers it."""

    status: int
    title: str
    detail: str
    sqlstate: str


def shortage(exc: BaseException | None) -> Shortage | None:
    """The resource the database ran short of, or its connection lost, when
    that is why ``exc`` happened: its own SQLSTATE, or that of an exception it
    was raised from (graphos wraps database errors, e.g. in ``CypherError``)."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        code = getattr(exc, "sqlstate", None)
        if isinstance(code, str) and code.startswith("53"):
            status, title, detail = _KNOWN.get(code, _OTHER)
            return Shortage(status, title, detail.format(sqlstate=code), code)
        if isinstance(code, str) and (code.startswith("08") or code in _GONE_CODES):
            status, title, detail = _GONE
            return Shortage(status, title, detail.format(sqlstate=code), code)
        if isinstance(exc, (ConnectionError, socket.gaierror)) and _raised_by_driver(exc):
            # The server down or its host gone: PostgreSQL's "unable to connect".
            status, title, detail = _GONE
            return Shortage(status, title, detail.format(sqlstate="08001"), "08001")
        exc = exc.__cause__ or exc.__context__
    return None


def _raised_by_driver(exc: BaseException) -> bool:
    """Whether the database driver raised ``exc``, not an HTTP client
    reaching another service (the identity provider) the same way."""
    tb = exc.__traceback__
    while tb is not None:
        if "asyncpg" in tb.tb_frame.f_code.co_filename:
            return True
        tb = tb.tb_next
    return False


class ShortageError(HTTPException):
    """A request the database could not serve for want of a resource, raised
    where a route answers its own database errors; the app answers it as
    ``application/problem+json``, like a shortage no route caught."""

    def __init__(self, found: Shortage) -> None:
        super().__init__(status_code=found.status, detail=found.detail)
        self.shortage = found


def write_failed(exc: BaseException, status_code: int, detail: str) -> HTTPException:
    """The answer to a write the database refused: the shortage's (507 or 503,
    without the database's message) when it ran short of a resource, else
    ``status_code`` with ``detail``. The write paths answer their own errors,
    so the app-wide handler never sees these."""
    found = shortage(exc)
    if found is not None:
        return ShortageError(found)
    return HTTPException(status_code=status_code, detail=detail)

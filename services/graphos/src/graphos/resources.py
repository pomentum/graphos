"""The database short of a resource: disk, memory, connections.

PostgreSQL reports these as SQLSTATE class 53. graphos answers them as what
they are (507 for a full disk, 503 for the rest) instead of an internal
error, so a caller, a job's record and the web UI can tell a full disk from
a bug. The database's own message names files and object ids: it goes to the
server log, never to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class Shortage:
    """What the database ran short of, as graphos answers it."""

    status: int
    title: str
    detail: str
    sqlstate: str


def shortage(exc: BaseException | None) -> Shortage | None:
    """The resource the database ran short of, when that is why ``exc``
    happened: its own SQLSTATE, or that of an exception it was raised from
    (graphos wraps database errors, e.g. in ``CypherError``)."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        code = getattr(exc, "sqlstate", None)
        if isinstance(code, str) and code.startswith("53"):
            status, title, detail = _KNOWN.get(code, _OTHER)
            return Shortage(status, title, detail.format(sqlstate=code), code)
        exc = exc.__cause__ or exc.__context__
    return None

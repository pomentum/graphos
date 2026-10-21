"""A database short of disk, memory or connections (SQLSTATE class 53) is
answered as such, 507 or 503, not as an internal error, and without the
database's own message, which names the server's files."""

from __future__ import annotations

import contextlib
from http import HTTPStatus
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from graphos import resources
from graphos.api.graph import _read_failed
from graphos.app import create_app
from graphos.config import Settings
from graphos.console import Console, ConsoleError
from graphos.cypher import CypherError
from graphos.jobs import _unexpected

DISK_FULL = 'could not extend file "base/16384/2619": No space left on device'


def _disk_full() -> asyncpg.exceptions.DiskFullError:
    return asyncpg.exceptions.DiskFullError(DISK_FULL)


def _wrapped(inner: BaseException) -> CypherError:
    """A database error as graphos's write path raises it: wrapped, from it."""
    try:
        raise inner
    except BaseException as exc:  # noqa: BLE001 -- building the chain under test
        try:
            raise CypherError(str(exc), sqlstate=getattr(exc, "sqlstate", None)) from exc
        except CypherError as wrapped:
            return wrapped
    raise AssertionError("unreachable")


# ── recognising a shortage ─────────────────────────────────────────────────


def test_a_full_disk_is_507_and_says_so_without_the_file() -> None:
    found = resources.shortage(_disk_full())
    assert found is not None
    assert (found.status, found.sqlstate) == (HTTPStatus.INSUFFICIENT_STORAGE, "53100")
    assert "out of disk space" in found.detail
    assert "base/" not in found.detail


@pytest.mark.parametrize(("error", "sqlstate"), [
    (asyncpg.exceptions.OutOfMemoryError, "53200"),
    (asyncpg.exceptions.TooManyConnectionsError, "53300"),
    (asyncpg.exceptions.ConfigurationLimitExceededError, "53400"),
    (asyncpg.exceptions.InsufficientResourcesError, "53000"),
])
def test_other_shortages_are_503(error: type[asyncpg.PostgresError], sqlstate: str) -> None:
    found = resources.shortage(error("short"))
    assert found is not None
    assert (found.status, found.sqlstate) == (HTTPStatus.SERVICE_UNAVAILABLE, sqlstate)


def test_a_shortage_is_found_through_what_wrapped_it() -> None:
    assert resources.shortage(CypherError(DISK_FULL, sqlstate="53100")) is not None
    plain = RuntimeError("the job failed")
    plain.__cause__ = _wrapped(_disk_full())
    found = resources.shortage(plain)
    assert found is not None
    assert found.sqlstate == "53100"


def test_anything_else_is_not_a_shortage() -> None:
    assert resources.shortage(None) is None
    assert resources.shortage(RuntimeError("boom")) is None
    assert resources.shortage(asyncpg.exceptions.UndefinedTableError("no table")) is None
    looped = RuntimeError("a")
    looped.__context__ = RuntimeError("b")
    looped.__context__.__context__ = looped
    assert resources.shortage(looped) is None


# ── where it is answered ───────────────────────────────────────────────────


def test_a_request_that_fills_the_disk_answers_507_problem_json() -> None:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))

    @app.get("/full")
    async def full() -> None:
        raise _wrapped(_disk_full())

    @app.get("/bug")
    async def bug() -> None:
        raise RuntimeError("a bug")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/full")
    assert resp.status_code == HTTPStatus.INSUFFICIENT_STORAGE
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert (body["status"], body["title"]) == (507, "Database out of disk space")
    assert "base/" not in resp.text
    # Anything else stays the plain internal error it was.
    resp = client.get("/bug")
    assert resp.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert resp.text == "Internal Server Error"


def test_a_job_that_fills_the_disk_records_it() -> None:
    assert _unexpected(_wrapped(_disk_full())) == {
        "status": 507, "detail": resources.shortage(_disk_full()).detail,  # type: ignore[union-attr]
    }
    assert _unexpected(RuntimeError("a bug")) == {
        "status": 500, "detail": "internal error; see the server log",
    }


def test_a_read_that_fills_the_disk_answers_507() -> None:
    exc = _read_failed("expand (hop 2)", _disk_full())
    assert exc.status_code == HTTPStatus.INSUFFICIENT_STORAGE
    assert "out of disk space" in str(exc.detail)
    other = _read_failed("expand (hop 2)", asyncpg.exceptions.UndefinedTableError("x"))
    assert other.status_code == HTTPStatus.INTERNAL_SERVER_ERROR


@pytest.mark.asyncio
async def test_a_console_query_that_fills_the_disk_answers_507_not_the_databases_message() -> None:
    class Conn:
        def get_server_pid(self) -> int:
            return 42

        def transaction(self, *, readonly: bool) -> contextlib.AbstractAsyncContextManager[None]:
            return contextlib.nullcontext()  # type: ignore[return-value]

        async def execute(self, sql: str) -> None:
            return None

        async def fetch(self, sql: str, params: str) -> list[Any]:
            raise _disk_full()

    async def gone() -> bool:
        return False

    console = Console(pool=None, owner=None, settings=Settings(db_password="x"))  # type: ignore[arg-type]
    with pytest.raises(ConsoleError) as caught:
        await console._on(Conn(), "t", "MATCH (n) RETURN n", "$$", ["n"], None, 10, gone)  # type: ignore[arg-type]
    assert caught.value.status == HTTPStatus.INSUFFICIENT_STORAGE
    assert "out of disk space" in str(caught.value)
    assert "base/" not in str(caught.value)

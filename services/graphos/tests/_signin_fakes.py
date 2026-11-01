"""A scripted stand-in for an asyncpg pool, for the sign-in and token tests.

No database: a test says what a statement answers (by a fragment of its SQL)
and reads back what was run.  Rows are plain dicts, which support the
``row["column"]`` and ``dict(row)`` uses of the code under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from graphos.identity import Signer, User

_DEFAULTS: dict[str, Any] = {"execute": "OK", "fetch": [], "fetchrow": None, "fetchval": None}


@dataclass
class Call:
    method: str
    sql: str
    args: tuple[Any, ...]


@dataclass
class FakePool:
    """Answers statements by the first rule whose fragment the SQL contains."""

    rules: list[tuple[str, Any]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    transactions: list[str] = field(default_factory=list)   # "commit" / "rollback"

    def on(self, fragment: str, answer: Any) -> FakePool:
        """``answer``: a value, or a callable of the statement's arguments."""
        self.rules.append((fragment, answer))
        return self

    def ran(self, fragment: str) -> list[Call]:
        return [c for c in self.calls if fragment in c.sql]

    def _run(self, method: str, sql: str, args: tuple[Any, ...]) -> Any:
        self.calls.append(Call(method, sql, args))
        for fragment, answer in self.rules:
            if fragment in sql:
                return answer(*args) if callable(answer) else answer
        return _DEFAULTS[method]

    # The pool's own shortcuts and a connection share the rules.
    async def execute(self, sql: str, *args: Any) -> Any:
        return self._run("execute", sql, args)

    async def fetch(self, sql: str, *args: Any) -> Any:
        return self._run("fetch", sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self._run("fetchrow", sql, args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self._run("fetchval", sql, args)

    def acquire(self) -> _Acquired:
        return _Acquired(FakeConn(self))


class FakeConn:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def execute(self, sql: str, *args: Any) -> Any:
        return self._pool._run("execute", sql, args)

    async def fetch(self, sql: str, *args: Any) -> Any:
        return self._pool._run("fetch", sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self._pool._run("fetchrow", sql, args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self._pool._run("fetchval", sql, args)

    def transaction(self) -> _Transaction:
        return _Transaction(self._pool)


class _Acquired:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> FakeConn:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Transaction:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, *_: object) -> None:
        self._pool.transactions.append("rollback" if exc_type else "commit")


def user_row(name: str = "alice", **extra: Any) -> dict[str, Any]:
    """A ``graphos_meta.users`` row as ``AccessStore.load`` reads it."""
    return {
        "name": name, "kind": "person", "must_change": False, "disabled": False,
        "token_version": 1, "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "created_by": "bootstrap", "last_login_at": None, **extra,
    }


def grant_row(subject: str, pattern: str, level: str) -> dict[str, Any]:
    return {
        "subject": subject, "tenant_pattern": pattern, "level": level,
        "granted_by": "bootstrap", "granted_at": datetime(2026, 1, 1, tzinfo=UTC),
    }


def make_user(name: str = "alice", **extra: Any) -> User:
    return User(**user_row(name, **extra))


def make_signer(issuer: str = "graphos", ttl_s: int = 900, kid: str = "k1") -> Signer:
    signer = Signer(issuer=issuer, ttl_s=ttl_s)
    signer.use_key(kid, ec.generate_private_key(ec.SECP256R1()))
    return signer


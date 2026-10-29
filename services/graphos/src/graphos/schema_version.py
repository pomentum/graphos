"""A tenant's schema version as an ETag, and schema writes conditional on it.

Two people editing one tenant's schema in the web UI each write whole entries
(a label's style, a property definition) built from the schema they loaded;
without a check the later save silently undid the earlier one. graphos now
serves the schema's version as an ``ETag`` (on ``GET /v1/{tenant}/schema``,
``GET /v1/tenants/{name}`` and the exported document) and every schema write
takes ``If-Match``: a write made from an older version answers 412, with the
current ``ETag``, and changes nothing. ``If-Match`` stays optional, so a
program that registers its schema on every start is unaffected.

The version is the one replicas already compare to reload a tenant
(``bootstrap.TENANT_ROWS_SQL``: the md5 of the stored spec, the declared pairs
and the settings), so any change to any of them changes it.

Every schema write holds the tenant's schema-write lock from the check to its
commit, with or without ``If-Match``: an unchecked write must not slip in
between another write's check and its write. The lock is a session advisory
lock on a key of its own (not the key ``bootstrap_tenant`` and the renames
take, which the write takes inside it), always taken first, so it cannot
deadlock with them. In front of it an in-process lock per tenant keeps
concurrent writes of one tenant on one replica from each holding a pooled
connection while they wait.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from graphos.bootstrap import TENANT_ROWS_SQL
from graphos.lookup import IfMatch

_VERSION_SQL = f"SELECT version FROM ({TENANT_ROWS_SQL}) r WHERE r.name = $1;"
_LOCK_KEY = "graphos.schema-write:"
_local: dict[str, asyncio.Lock] = {}


def etag(version: str) -> str:
    """A strong ETag for a schema version."""
    return f'"{version}"'


async def current(conn: asyncpg.Connection, tenant: str) -> str | None:
    """The tenant's schema ETag now, or ``None`` when it is not registered."""
    version = await conn.fetchval(_VERSION_SQL, tenant)
    return None if version is None else etag(str(version))


class SchemaWrite:
    """A schema write in progress, under the tenant's lock."""

    def __init__(self, conn: asyncpg.Connection, tenant: str) -> None:
        self._conn = conn
        self._tenant = tenant

    async def etag(self) -> str | None:
        """The schema's ETag as the write left it."""
        return await current(self._conn, self._tenant)


@asynccontextmanager
async def write(
    pool: asyncpg.Pool, tenant: str, if_match: IfMatch | None,
) -> AsyncIterator[SchemaWrite]:
    """Hold the tenant's schema-write lock; check ``if_match`` first.

    Raises :class:`graphos.lookup.PreconditionFailedError` (with the current
    ETag, ``None`` when the tenant is not registered) when ``if_match`` does
    not match; nothing is written then.
    """
    local = _local.setdefault(tenant, asyncio.Lock())
    async with local, pool.acquire() as conn:
        key = _LOCK_KEY + tenant
        await conn.execute("SELECT pg_advisory_lock(hashtextextended($1, 0));", key)
        try:
            if if_match is not None:
                if_match.check(await current(conn, tenant))
            yield SchemaWrite(conn, tenant)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0));", key)


async def check(pool: asyncpg.Pool, tenant: str, if_match: IfMatch | None) -> None:
    """Check ``if_match`` without writing (a dry run)."""
    if if_match is None:
        return
    async with pool.acquire() as conn:
        if_match.check(await current(conn, tenant))

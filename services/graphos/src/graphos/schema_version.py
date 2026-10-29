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
import contextlib
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from graphos.bootstrap import TENANT_ROWS_SQL
from graphos.lookup import IfMatch, PreconditionFailedError

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


class SchemaBusyError(Exception):
    """Another schema write of the tenant held its lock longer than the caller
    waits for it (answered 409 by graphos.app)."""

    def __init__(self, tenant: str) -> None:
        super().__init__(
            f"a schema write on '{tenant}' is in progress; try again when it ends",
        )
        self.tenant = tenant


#: How long unregistering, restoring and deleting wait for a schema write in
#: progress (a registration building indexes, say) before answering 409. A
#: registration itself waits as long as it takes, as it always has.
LIFECYCLE_WAIT_S = 30.0

# Schema writers per event loop hold at most half the pool at once: each holds
# its lock's connection and takes more inside, so a burst of them on many
# tenants must not take every connection and wait for one more.
_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _gate(pool: asyncpg.Pool) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        size = pool.get_max_size() if hasattr(pool, "get_max_size") else 2
        gate = _gates[loop] = asyncio.Semaphore(max(1, size // 2))
    return gate


@asynccontextmanager
async def write(
    pool: asyncpg.Pool, tenant: str, if_match: IfMatch | None, *, absent: bool = False,
    wait_s: float | None = None,
) -> AsyncIterator[SchemaWrite]:
    """Hold the tenant's schema-write lock; check ``if_match`` first, and with
    ``absent`` (``If-None-Match: *``) that the tenant is not registered.

    Raises :class:`graphos.lookup.PreconditionFailedError` (with the current
    ETag, ``None`` when the tenant is not registered) when either does not
    hold; nothing is written then. Raises :class:`SchemaBusyError` when the
    lock is not had within ``wait_s`` seconds (``None``: wait), or when the
    wait outlasts the database's statement timeout.
    """
    loop = asyncio.get_running_loop()
    deadline = None if wait_s is None else loop.time() + wait_s
    local = _local.setdefault(tenant, asyncio.Lock())
    async with contextlib.AsyncExitStack() as stack:
        try:
            async with asyncio.timeout_at(deadline):
                await stack.enter_async_context(local)
                await stack.enter_async_context(_gate(pool))
                conn = await stack.enter_async_context(pool.acquire())
        except TimeoutError as exc:
            raise SchemaBusyError(tenant) from exc
        key = _LOCK_KEY + tenant
        await _lock(conn, key, tenant, None if deadline is None else deadline - loop.time())
        try:
            if if_match is not None:
                if_match.check(await current(conn, tenant))
            if absent and (now := await current(conn, tenant)) is not None:
                raise PreconditionFailedError(now)
            yield SchemaWrite(conn, tenant)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0));", key)


async def _lock(conn: asyncpg.Connection, key: str, tenant: str, wait_s: float | None) -> None:
    """Take the advisory lock, waiting at most ``wait_s`` (the database's
    ``lock_timeout``, set for this wait only); :class:`SchemaBusyError` when
    it is not had."""
    if wait_s is None:
        try:
            await conn.execute("SELECT pg_advisory_lock(hashtextextended($1, 0));", key)
        except asyncpg.QueryCanceledError as exc:   # the statement timeout
            raise SchemaBusyError(tenant) from exc
        return
    before = await conn.fetchval("SELECT current_setting('lock_timeout');")
    await conn.execute(
        "SELECT set_config('lock_timeout', $1, false);", f"{max(1, int(wait_s * 1000))}ms",
    )
    try:
        await conn.execute("SELECT pg_advisory_lock(hashtextextended($1, 0));", key)
    except (asyncpg.LockNotAvailableError, asyncpg.QueryCanceledError) as exc:
        raise SchemaBusyError(tenant) from exc
    finally:
        await conn.execute("SELECT set_config('lock_timeout', $1, false);", before)

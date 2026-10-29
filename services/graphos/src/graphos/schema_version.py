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
    """A schema write waited longer than it may for its turn (answered 409,
    ``code: schema_busy``, by graphos.app)."""

    def __init__(self, tenant: str, why: str) -> None:
        super().__init__(f"{why}; try again when it ends")
        self.tenant = tenant


#: How long a schema write waits for its turn behind another schema write of
#: the tenant (on any replica) before answering 409: a registration building
#: indexes may hold it for minutes.
WRITE_WAIT_S = 300.0
#: The same for unregistering, restoring and deleting a tenant.
LIFECYCLE_WAIT_S = 30.0
# Between two tries of the advisory lock: no statement stays open while
# waiting, so a waiter's snapshot never holds up the holder's concurrent index
# builds (which wait for every older snapshot).
_POLL_S = (0.02, 0.5)


class _TenantLocks:
    """This process's lock per tenant, for one event loop; an entry lives
    while someone holds or waits for it."""

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @contextlib.asynccontextmanager
    async def hold(self, tenant: str) -> AsyncIterator[None]:
        lock, users = self._locks.get(tenant, (asyncio.Lock(), 0))
        self._locks[tenant] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[tenant]
            if users == 1:
                del self._locks[tenant]
            else:
                self._locks[tenant] = (lock, users - 1)


# Per event loop (an asyncio lock serves one loop): the tenants' locks, and the
# gate in front of the pool. Schema writers hold at most half the pool at once:
# each holds its lock's connection and takes more inside, so a burst of them on
# many tenants must not take every connection and then wait for one more.
_locals: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _TenantLocks] = (
    weakref.WeakKeyDictionary()
)
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
    wait_s: float = WRITE_WAIT_S,
) -> AsyncIterator[SchemaWrite]:
    """Hold the tenant's schema-write lock; check ``if_match`` first, and with
    ``absent`` (``If-None-Match: *``) that the tenant is not registered.

    Raises :class:`graphos.lookup.PreconditionFailedError` (with the current
    ETag, ``None`` when the tenant is not registered) when either does not
    hold; nothing is written then. Raises :class:`SchemaBusyError` when the
    lock is not had within ``wait_s`` seconds.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    locks = _locals.setdefault(loop, _TenantLocks())
    async with contextlib.AsyncExitStack() as stack:
        waiting = f"another schema write of '{tenant}' is in progress"
        try:
            async with asyncio.timeout_at(deadline) as turn:
                await stack.enter_async_context(locks.hold(tenant))
                waiting = "schema writes of other tenants hold this replica's connections"
                await stack.enter_async_context(_gate(pool))
        except TimeoutError as exc:
            if turn.expired():
                raise SchemaBusyError(tenant, waiting) from exc
            raise
        conn = await stack.enter_async_context(pool.acquire())
        key = _LOCK_KEY + tenant
        await _lock(conn, key, tenant, deadline)
        try:
            if if_match is not None:
                if_match.check(await current(conn, tenant))
            if absent and (now := await current(conn, tenant)) is not None:
                raise PreconditionFailedError(now)
            yield SchemaWrite(conn, tenant)
        finally:
            # A cancelled unlock is undone anyway: the pool's reset on release
            # runs pg_advisory_unlock_all (graphos.db keeps its default reset).
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0));", key)


async def _lock(conn: asyncpg.Connection, key: str, tenant: str, deadline: float) -> None:
    """Take the tenant's advisory lock (held by a schema write on any replica),
    trying until ``deadline``; :class:`SchemaBusyError` when it is not had."""
    loop = asyncio.get_running_loop()
    pause = _POLL_S[0]
    while not await conn.fetchval(
        "SELECT pg_try_advisory_lock(hashtextextended($1, 0));", key,
    ):
        if loop.time() + pause > deadline:
            raise SchemaBusyError(
                tenant, f"another schema write of '{tenant}' (on another replica) is in progress",
            )
        await asyncio.sleep(pause)
        pause = min(pause * 2, _POLL_S[1])

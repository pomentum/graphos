"""Keeps each replica's in-process state in step with the database.

graphos keeps a few things in process memory for speed: the tenant
registry (``graphos.bootstrap``), the resolver catalogue and the K/V cache's
LRU.  With several API replicas, a change made through one must reach the
others.  Each change is announced with a Postgres ``NOTIFY``; every replica
listens on one connection (:class:`Listener`) and reloads or evicts the
affected entry.

A notification is only a shortcut.  Every piece of state also converges
without one, so a lost notification or a dropped listener connection
delays a change instead of hiding it:

* the registry is reconciled against ``graphos_meta.tenants`` every
  ``GRAPHOS_REGISTRY_SYNC_S`` seconds and whenever the listener
  (re)connects, and a tenant this replica does not know is looked up in the
  database before it is answered 404 (:meth:`RegistrySync.ensure`);
* an LRU entry is served for at most ``GRAPHOS_CACHE_MAX_AGE_S`` seconds,
  and the LRU is cleared when the listener (re)connects.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Callable

import asyncpg

from graphos import bootstrap

log = logging.getLogger(__name__)

TENANTS_CHANNEL = bootstrap.TENANTS_CHANNEL
CACHE_CHANNEL = "graphos_cache"

# Identifies this process in its own cache notifications, so it does not
# evict the entry it has just written.
INSTANCE_ID = uuid.uuid4().hex


async def notify_cache(conn: asyncpg.Connection, tenant: str, key: str | None) -> None:
    """Announce that a cache key changed (``None``: every key of the tenant),
    for the other replicas' LRUs."""
    payload = json.dumps({"tenant": tenant, "key": key, "by": INSTANCE_ID})
    await conn.execute("SELECT pg_notify($1, $2);", CACHE_CHANNEL, payload)


class Listener:
    """One ``LISTEN`` connection per process, shared by every subscriber.

    ``subscribe(channel, callback)`` before :meth:`start`; ``on_connect``
    callbacks run each time the connection is (re)established, after the
    channels are listened on, so a subscriber can catch up on what it may
    have missed.  The connection is retried every 2 s while it is down.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._channels: dict[str, list[Callable[[str], None]]] = {}
        self._on_connect: list[Callable[[], object]] = []
        self._task: asyncio.Task[None] | None = None
        self.listening = asyncio.Event()

    def subscribe(self, channel: str, callback: Callable[[str], None]) -> None:
        self._channels.setdefault(channel, []).append(callback)

    def on_connect(self, callback: Callable[[], object]) -> None:
        """``callback()`` may return an awaitable; it is awaited."""
        self._on_connect.append(callback)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="graphos-listener")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def _dispatch(self, _conn: object, _pid: int, channel: str, payload: str) -> None:
        for callback in self._channels.get(channel, ()):
            try:
                callback(payload)
            except Exception:  # noqa: BLE001 — one subscriber must not stop the others
                log.exception("notification handler failed", extra={"channel": channel})

    async def _run(self) -> None:
        while True:
            try:
                async with self._pool.acquire() as conn:
                    lost = asyncio.Event()
                    conn.add_termination_listener(lambda _c, lost=lost: lost.set())
                    for channel in self._channels:
                        await conn.add_listener(channel, self._dispatch)
                    self.listening.set()
                    for callback in self._on_connect:
                        result = callback()
                        if asyncio.iscoroutine(result):
                            await result
                    try:
                        await lost.wait()
                    finally:
                        self.listening.clear()
                        for channel in self._channels:
                            with contextlib.suppress(Exception):
                                await conn.remove_listener(channel, self._dispatch)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — keep trying; the fallbacks cover the gap
                log.warning("listener lost its connection; retrying", exc_info=True)
            await asyncio.sleep(2.0)


class RegistrySync:
    """Keeps :func:`graphos.bootstrap.registered_tenants` equal to the table.

    A tenant's version is the md5 of its stored spec, declared endpoints and
    settings (``bootstrap.TENANT_ROWS_SQL``), so any change to them is seen,
    whatever the clock says.
    """

    # How long "no such tenant" is remembered, so requests for unknown
    # names cannot each cost a database query.
    NEGATIVE_TTL_S = 5.0
    # Unknown names remembered at most, so they cannot grow memory unbounded.
    NEGATIVE_MAX = 10_000

    def __init__(self, pool: asyncpg.Pool, listener: Listener, *, interval_s: float) -> None:
        self._pool = pool
        self._interval_s = interval_s
        self._versions: dict[str, str] = {}
        self._unknown: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None
        listener.subscribe(TENANTS_CHANNEL, self._notified)
        listener.on_connect(self.reconcile)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="graphos-registry-sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def _notified(self, name: str) -> None:
        self._unknown.pop(name, None)
        asyncio.get_running_loop().create_task(self._apply_logged(name))

    async def _apply_logged(self, name: str) -> None:
        try:
            await self.apply(name)
        except Exception:  # noqa: BLE001 — the periodic reconcile will catch up
            log.warning("tenant reload failed", extra={"tenant": name}, exc_info=True)

    async def apply(self, name: str) -> bool:
        """Load tenant ``name`` from the table (or forget it).  True if it exists."""
        row = await self._pool.fetchrow(bootstrap.TENANT_ROWS_SQL + " WHERE t.name = $1;", name)
        if row is None:
            if bootstrap.forget(name):
                log.info("tenant unregistered by another replica", extra={"tenant": name})
            self._versions.pop(name, None)
            return False
        if self._versions.get(name) != row["version"]:
            bootstrap.remember(bootstrap.spec_from_row(row))
            self._versions[name] = row["version"]
            log.info("tenant registration loaded", extra={"tenant": name})
        return True

    async def reconcile(self) -> int:
        """Bring the whole registry up to date.  Returns the tenants changed."""
        rows = await self._pool.fetch(bootstrap.TENANT_ROWS_SQL + ";")
        changed = 0
        present = set()
        for row in rows:
            name = row["name"]
            present.add(name)
            if self._versions.get(name) != row["version"] or name not in (
                bootstrap.registered_tenants()
            ):
                bootstrap.remember(bootstrap.spec_from_row(row))
                self._versions[name] = row["version"]
                changed += 1
        for name in set(bootstrap.registered_tenants()) - present:
            bootstrap.forget(name)
            self._versions.pop(name, None)
            changed += 1
        if changed:
            log.info("tenant registry reconciled", extra={"changed": changed})
        return changed

    async def ensure(self, name: str) -> None:
        """Make sure a tenant registered on another replica is known here.

        Cheap when the tenant is known (a dict lookup), one query when it is
        not, and nothing for a name found missing within the last few
        seconds.
        """
        if name in bootstrap.registered_tenants():
            return
        deadline = self._unknown.get(name)
        if deadline is not None and time.monotonic() < deadline:
            return
        if not await self.apply(name):
            self._unknown[name] = time.monotonic() + self.NEGATIVE_TTL_S
            if len(self._unknown) > self.NEGATIVE_MAX:
                self._unknown.clear()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — try again next round
                log.warning("tenant registry reconcile failed", exc_info=True)

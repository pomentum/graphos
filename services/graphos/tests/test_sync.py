"""graphos.sync in one process: what another replica's changes do here.

DB-gated.  "Another replica" is simulated by writing ``graphos_meta.tenants``
directly, with or without the notification, which is all another process
does.  The two-process version is ``test_replicas.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from graphos import bootstrap, sync
from graphos.api import cache

from .conftest import requires_db


def _name() -> str:
    return f"sync_{uuid4().hex[:10]}"


def _spec(name: str, *labels: str) -> str:
    return bootstrap.spec_to_json(bootstrap.TenantSpec(
        name=name, vertex_labels=labels or ("Doc",), edge_labels=(),
    ))


async def _write(pool: asyncpg.Pool, name: str, spec: str, *, notify: bool) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO graphos_meta.tenants (name, spec) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (name) DO UPDATE SET spec = EXCLUDED.spec, updated_at = now();",
            name, spec,
        )
        if notify:
            await conn.execute("SELECT pg_notify($1, $2);", sync.TENANTS_CHANNEL, name)


async def _delete(pool: asyncpg.Pool, name: str, *, notify: bool) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", name)
        await conn.execute(
            "DELETE FROM graphos_meta.unregistered_tenants WHERE name = $1;", name,
        )
        if notify:
            await conn.execute("SELECT pg_notify($1, $2);", sync.TENANTS_CHANNEL, name)


async def _until(check, timeout: float = 5.0) -> float:  # noqa: ANN001
    started = time.monotonic()
    while not check():
        assert time.monotonic() - started < timeout, "not applied in time"
        await asyncio.sleep(0.02)
    return time.monotonic() - started


@pytest_asyncio.fixture
async def synced(db_pool: asyncpg.Pool) -> AsyncIterator[sync.RegistrySync]:
    listener = sync.Listener(db_pool)
    registry = sync.RegistrySync(db_pool, listener, interval_s=3600)
    listener.start()
    await asyncio.wait_for(listener.listening.wait(), timeout=10)
    try:
        yield registry
    finally:
        await listener.stop()


@requires_db
@pytest.mark.asyncio
async def test_notifications_apply_another_replicas_changes(
    db_pool: asyncpg.Pool, synced: sync.RegistrySync,
) -> None:
    name = _name()
    try:
        await _write(db_pool, name, _spec(name, "Doc"), notify=True)
        took = await _until(lambda: name in bootstrap.registered_tenants())
        assert took < 2

        await _write(db_pool, name, _spec(name, "Doc", "Tag"), notify=True)
        await _until(lambda: "Tag" in bootstrap.registered_tenants()[name].vertex_labels)

        await _delete(db_pool, name, notify=True)
        await _until(lambda: name not in bootstrap.registered_tenants())
    finally:
        bootstrap.forget(name)
        await _delete(db_pool, name, notify=False)


@requires_db
@pytest.mark.asyncio
async def test_reconcile_catches_what_a_lost_notification_missed(
    db_pool: asyncpg.Pool, synced: sync.RegistrySync,
) -> None:
    added, dropped, changed = _name(), _name(), _name()
    try:
        await _write(db_pool, dropped, _spec(dropped), notify=False)
        await _write(db_pool, changed, _spec(changed, "Doc"), notify=False)
        await synced.reconcile()
        await _write(db_pool, added, _spec(added), notify=False)
        await _delete(db_pool, dropped, notify=False)
        await _write(db_pool, changed, _spec(changed, "Doc", "Tag"), notify=False)
        await asyncio.sleep(0.3)
        known = bootstrap.registered_tenants()
        assert added not in known and dropped in known      # nothing was announced

        assert await synced.reconcile() >= 3
        known = bootstrap.registered_tenants()
        assert added in known and dropped not in known
        assert known[changed].vertex_labels == ("Doc", "Tag")
        assert await synced.reconcile() == 0                # nothing left to do
    finally:
        for n in (added, dropped, changed):
            bootstrap.forget(n)
            await _delete(db_pool, n, notify=False)


@requires_db
@pytest.mark.asyncio
async def test_an_unknown_tenant_is_looked_up_once(db_pool: asyncpg.Pool) -> None:
    class Counting:
        """The pool, counting the lookups ``ensure`` makes."""

        def __init__(self, pool: asyncpg.Pool) -> None:
            self.pool, self.queries = pool, 0

        async def fetchrow(self, *args: object) -> object:
            self.queries += 1
            return await self.pool.fetchrow(*args)

    counting = Counting(db_pool)
    registry = sync.RegistrySync(counting, sync.Listener(db_pool), interval_s=3600)
    name, missing = _name(), _name()
    try:
        await _write(db_pool, name, _spec(name), notify=False)
        await registry.ensure(name)                         # registered elsewhere, unannounced
        assert name in bootstrap.registered_tenants() and counting.queries == 1
        await registry.ensure(name)
        assert counting.queries == 1                        # known: no query

        await registry.ensure(missing)
        await registry.ensure(missing)
        assert missing not in bootstrap.registered_tenants()
        assert counting.queries == 2                        # remembered as missing
        registry._unknown[missing] = time.monotonic() - 1    # noqa: SLF001 — let it expire
        await registry.ensure(missing)
        assert counting.queries == 3                        # asked again once it expired
    finally:
        bootstrap.forget(name)
        await _delete(db_pool, name, notify=False)


@requires_db
@pytest.mark.asyncio
async def test_registration_through_graphos_announces_itself(db_pool: asyncpg.Pool) -> None:
    name = _name()
    heard: list[str] = []
    conn = await asyncpg.connect(*db_pool._connect_args, **db_pool._connect_kwargs)  # noqa: SLF001
    try:
        await conn.add_listener(sync.TENANTS_CHANNEL, lambda *a: heard.append(a[3]))
        spec = bootstrap.TenantSpec(name=name, vertex_labels=("Doc",), edge_labels=())
        await bootstrap.register_tenant(db_pool, spec)
        await bootstrap.unregister_tenant(db_pool, name)
        await _until(lambda: heard.count(name) >= 2)
    finally:
        await conn.close()
        bootstrap.forget(name)


# ── the K/V cache ──────────────────────────────────────────────────────────


def test_cache_notifications_evict_other_replicas_entries_only() -> None:
    lru = cache.CacheLRU()
    original = cache.LRU
    cache.LRU = lru
    try:
        lru.put("t", "k", 1, None)
        cache.on_notification(json.dumps({"tenant": "t", "key": "k", "by": sync.INSTANCE_ID}))
        assert lru.get("t", "k") is not None                # our own write
        cache.on_notification(json.dumps({"tenant": "t", "key": "k", "by": "other"}))
        assert lru.get("t", "k") is None                    # another replica's
        cache.on_notification("not json")                   # ignored
    finally:
        cache.LRU = original


def test_cache_entries_age_out() -> None:
    lru = cache.CacheLRU(max_age_s=0.05)
    lru.put("t", "k", 1, None)
    assert lru.get("t", "k") is not None
    time.sleep(0.08)
    assert lru.get("t", "k") is None

"""graphos.sync without a database: the shared LISTEN connection, the tenant
registry kept in step, and the cache evictions notifications carry.

The pool and its connections are fakes that keep listener callbacks and
can drop the connection, so reconnects, retries and dispatch are checked
here; ``test_sync.py`` and ``test_replicas.py`` run against a live database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from graphos import bootstrap, changes, sync
from graphos.api import cache
from graphos.bootstrap import TenantSpec

# ── Fakes ──────────────────────────────────────────────────────────────────


class _Conn:
    """A LISTEN connection: keeps callbacks, delivers, and can be lost."""

    def __init__(self) -> None:
        self.listeners: dict[str, list[Callable[..., None]]] = {}
        self.terminations: list[Callable[[object], None]] = []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def add_termination_listener(self, callback: Callable[[object], None]) -> None:
        self.terminations.append(callback)

    async def add_listener(self, channel: str, callback: Callable[..., None]) -> None:
        self.listeners.setdefault(channel, []).append(callback)

    async def remove_listener(self, channel: str, callback: Callable[..., None]) -> None:
        self.listeners.get(channel, []).remove(callback)

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "SELECT 1"

    def notify(self, channel: str, payload: str) -> None:
        for callback in list(self.listeners.get(channel, [])):
            callback(self, 4242, channel, payload)

    def lose(self) -> None:
        for callback in list(self.terminations):
            callback(self)


def _row(name: str, *labels: str, version: str | None = None) -> dict[str, Any]:
    spec = bootstrap.spec_to_json(TenantSpec(
        name=name, vertex_labels=labels or ("Doc",), edge_labels=(),
    ))
    return {
        "name": name, "spec": spec, "endpoints": "[]", "strict_endpoints": False,
        "schema_manager": None, "schema_manager_since": None,
        "version": version or hashlib.md5(spec.encode()).hexdigest(),  # noqa: S324
    }


class _Pool:
    """Hands out queued connections (or raises queued errors), then blocks;
    answers the registry's queries from ``rows``."""

    def __init__(self, *conns: _Conn | BaseException) -> None:
        self.queue: list[_Conn | BaseException] = list(conns)
        self.attempts = 0
        self.rows: dict[str, dict[str, Any]] = {}
        self.queries: list[str | None] = []
        self.fail_with: BaseException | None = None
        self.gates: list[asyncio.Future[None]] = []

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[_Conn]:
        self.attempts += 1
        if not self.queue:
            await asyncio.Event().wait()          # the database never comes back
        nxt = self.queue.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        yield nxt

    def acquire(self):  # noqa: ANN201
        return self._acquire()

    async def fetchrow(self, _sql: str, name: str) -> dict[str, Any] | None:
        self.queries.append(name)
        if self.gates:
            await self.gates.pop(0)
        if self.fail_with is not None:
            raise self.fail_with
        row = self.rows.get(name)
        return None if row is None else dict(row)

    async def fetch(self, _sql: str) -> list[dict[str, Any]]:
        self.queries.append(None)
        if self.fail_with is not None:
            raise self.fail_with
        return [dict(r) for r in self.rows.values()]


class _FastSleep:
    """``sync``'s view of asyncio, with sleeps recorded and not waited out."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> _FastSleep:
    fast = _FastSleep()
    monkeypatch.setattr(sync, "asyncio", fast)
    return fast


@pytest.fixture(autouse=True)
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, TenantSpec]:
    """A registry of this test's own, so nothing leaks between tests."""
    own: dict[str, TenantSpec] = {}
    monkeypatch.setattr(bootstrap, "_REGISTERED", own)
    return own


async def _until(check: Callable[[], bool], ticks: int = 500) -> None:
    for _ in range(ticks):
        if check():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


async def _started(listener: sync.Listener) -> None:
    listener.start()
    await _until(listener.listening.is_set)


# ── Listener: dispatch ─────────────────────────────────────────────────────


async def test_a_notification_reaches_every_subscriber_of_its_channel_only(
    sleeps: _FastSleep,  # noqa: ARG001
) -> None:
    conn = _Conn()
    listener = sync.Listener(_Pool(conn))  # type: ignore[arg-type]
    got: list[tuple[str, str]] = []
    listener.subscribe("alpha", lambda p: got.append(("a1", p)))
    listener.subscribe("alpha", lambda p: got.append(("a2", p)))
    listener.subscribe("beta", lambda p: got.append(("b", p)))
    await _started(listener)
    try:
        conn.notify("alpha", "acme")
        conn.notify("unknown", "ignored")
        assert got == [("a1", "acme"), ("a2", "acme")]
    finally:
        await listener.stop()


async def test_a_failing_subscriber_does_not_keep_the_notification_from_the_others(
    sleeps: _FastSleep, caplog: pytest.LogCaptureFixture,  # noqa: ARG001
) -> None:
    conn = _Conn()
    listener = sync.Listener(_Pool(conn))  # type: ignore[arg-type]
    got: list[str] = []

    def broken(_payload: str) -> None:
        raise RuntimeError("boom")

    listener.subscribe("alpha", broken)
    listener.subscribe("alpha", got.append)
    await _started(listener)
    try:
        conn.notify("alpha", "one")
        conn.notify("alpha", "two")
        assert got == ["one", "two"]
        assert listener.listening.is_set()
        assert sum("notification handler failed" in r.getMessage() for r in caplog.records) == 2
    finally:
        await listener.stop()


# ── Listener: connecting, reconnecting, stopping ───────────────────────────


async def test_catch_up_callbacks_run_once_the_channels_are_listened_on(
    sleeps: _FastSleep,  # noqa: ARG001
) -> None:
    conn = _Conn()
    listener = sync.Listener(_Pool(conn))  # type: ignore[arg-type]
    listener.subscribe("alpha", lambda _p: None)
    order: list[str] = []

    def plain() -> None:
        assert "alpha" in conn.listeners and listener.listening.is_set()
        order.append("plain")

    async def awaited() -> None:
        await asyncio.sleep(0)
        order.append("awaited")

    listener.on_connect(awaited)
    listener.on_connect(plain)
    await _started(listener)
    try:
        await _until(lambda: len(order) == 2)
        assert order == ["awaited", "plain"]      # the coroutine finished first
    finally:
        await listener.stop()


async def test_a_lost_connection_is_replaced_after_a_pause_and_everything_relistened(
    sleeps: _FastSleep,
) -> None:
    first, second = _Conn(), _Conn()
    pool = _Pool(first, second)
    listener = sync.Listener(pool)  # type: ignore[arg-type]
    got: list[str] = []
    connects: list[int] = []
    listener.subscribe("alpha", got.append)
    listener.on_connect(lambda: connects.append(pool.attempts))
    await _started(listener)
    try:
        first.lose()
        await _until(lambda: connects == [1, 2])
        assert first.listeners == {"alpha": []}       # unhooked from the dead connection
        assert sleeps.delays == [2.0]                 # one pause before reconnecting
        assert listener.listening.is_set()
        second.notify("alpha", "after")
        assert got == ["after"]
    finally:
        await listener.stop()


async def test_listening_is_cleared_while_the_connection_is_down(sleeps: _FastSleep) -> None:
    conn = _Conn()
    listener = sync.Listener(_Pool(conn))  # type: ignore[arg-type]
    listener.subscribe("alpha", lambda _p: None)
    await _started(listener)
    try:
        conn.lose()
        await _until(lambda: sleeps.delays == [2.0])
        assert not listener.listening.is_set()        # the pool has nothing to give
    finally:
        await listener.stop()


async def test_a_database_that_is_down_is_retried_with_a_pause_between_attempts(
    sleeps: _FastSleep, caplog: pytest.LogCaptureFixture,
) -> None:
    conn = _Conn()
    pool = _Pool(OSError("refused"), OSError("refused"), OSError("refused"), conn)
    listener = sync.Listener(pool)  # type: ignore[arg-type]
    listener.subscribe("alpha", lambda _p: None)
    await _started(listener)
    try:
        assert pool.attempts == 4
        assert sleeps.delays == [2.0, 2.0, 2.0]       # never a tight loop
        lost = [r for r in caplog.records if "listener lost its connection" in r.getMessage()]
        assert len(lost) == 3
    finally:
        await listener.stop()


async def test_stopping_releases_the_connection_and_clears_listening(
    sleeps: _FastSleep,  # noqa: ARG001
) -> None:
    conn = _Conn()
    listener = sync.Listener(_Pool(conn))  # type: ignore[arg-type]
    listener.subscribe("alpha", lambda _p: None)
    await listener.stop()                             # before start: nothing to do
    await _started(listener)
    await listener.stop()
    assert not listener.listening.is_set()
    assert conn.listeners == {"alpha": []}
    await listener.stop()                             # twice: still fine


async def test_a_failing_catch_up_callback_neither_stops_the_others_nor_the_listening(
    sleeps: _FastSleep,  # noqa: ARG001
) -> None:
    conn, spare = _Conn(), _Conn()
    listener = sync.Listener(_Pool(conn, spare))  # type: ignore[arg-type]
    got: list[str] = []
    later: list[str] = []
    listener.subscribe("alpha", got.append)

    def broken() -> None:
        raise RuntimeError("reconcile failed")

    listener.on_connect(broken)
    listener.on_connect(lambda: later.append("ran"))
    await _started(listener)
    try:
        for _ in range(20):
            await asyncio.sleep(0)
        assert later, "the callbacks after the failing one never ran"
        conn.notify("alpha", "acme")
        assert got == ["acme"], "the connection was dropped"
    finally:
        await listener.stop()


async def test_a_catch_up_callback_returning_a_future_is_awaited(
    sleeps: _FastSleep,  # noqa: ARG001
) -> None:
    conn = _Conn()
    listener = sync.Listener(_Pool(conn))  # type: ignore[arg-type]
    pending: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    later: list[str] = []
    listener.on_connect(lambda: pending)
    listener.on_connect(lambda: later.append("ran"))
    await _started(listener)
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        assert later == [], "ran before the first callback's future completed"
    finally:
        pending.set_result(None)
        await listener.stop()


# ── Cache evictions carried by notifications ───────────────────────────────


@pytest.fixture
def lru(monkeypatch: pytest.MonkeyPatch) -> cache.CacheLRU:
    fresh = cache.CacheLRU(capacity=100, max_age_s=3600)
    monkeypatch.setattr(cache, "LRU", fresh)
    return fresh


def _fill(lru: cache.CacheLRU) -> None:
    later = datetime.now(UTC) + timedelta(hours=1)
    for tenant, key in [("acme", "k1"), ("acme", "k2"), ("beta", "k1")]:
        lru.put(tenant, key, {"v": key}, later)


async def test_another_replicas_cache_change_evicts_the_key_or_the_whole_tenant(
    sleeps: _FastSleep, lru: cache.CacheLRU,  # noqa: ARG001
) -> None:
    conn = _Conn()
    listener = sync.Listener(_Pool(conn))  # type: ignore[arg-type]
    listener.subscribe(sync.CACHE_CHANNEL, cache.on_notification)
    await _started(listener)
    try:
        _fill(lru)
        other = "f" * 32
        conn.notify(sync.CACHE_CHANNEL, json.dumps({"tenant": "acme", "key": "k1", "by": other}))
        assert lru.get("acme", "k1") is None and lru.get("acme", "k2") is not None
        assert lru.get("beta", "k1") is not None
        conn.notify(sync.CACHE_CHANNEL, json.dumps({"tenant": "acme", "key": None, "by": other}))
        assert lru.get("acme", "k2") is None and lru.get("beta", "k1") is not None
    finally:
        await listener.stop()


async def test_this_replicas_own_cache_notification_and_garbage_evict_nothing(
    sleeps: _FastSleep, lru: cache.CacheLRU,  # noqa: ARG001
) -> None:
    conn = _Conn()
    listener = sync.Listener(_Pool(conn))  # type: ignore[arg-type]
    listener.subscribe(sync.CACHE_CHANNEL, cache.on_notification)
    await _started(listener)
    try:
        _fill(lru)
        await sync.notify_cache(conn, "acme", "k1")  # type: ignore[arg-type]
        (sql, (channel, payload)), = conn.executed
        assert "pg_notify" in sql and channel == sync.CACHE_CHANNEL
        assert json.loads(payload) == {"tenant": "acme", "key": "k1", "by": sync.INSTANCE_ID}
        conn.notify(channel, payload)                 # the echo of our own write
        conn.notify(channel, "{not json")
        assert lru.size() == 3
    finally:
        await listener.stop()


async def test_a_reconnect_clears_the_cache_in_case_an_eviction_was_missed(
    sleeps: _FastSleep, lru: cache.CacheLRU,  # noqa: ARG001
) -> None:
    first, second = _Conn(), _Conn()
    listener = sync.Listener(_Pool(first, second))  # type: ignore[arg-type]
    listener.subscribe(sync.CACHE_CHANNEL, cache.on_notification)
    listener.on_connect(cache.LRU.clear)
    await _started(listener)
    try:
        _fill(lru)
        first.lose()
        await _until(lambda: lru.size() == 0)
    finally:
        await listener.stop()


# ── The change feed's wake-ups through the shared listener ────────────────


async def test_a_change_notification_wakes_only_that_tenants_streams(
    sleeps: _FastSleep,  # noqa: ARG001
) -> None:
    conn = _Conn()
    listener = sync.Listener(_Pool(conn))  # type: ignore[arg-type]
    feed = changes.ChangeFeed(object(), listener, retention_h=1, poll_s=30)  # type: ignore[arg-type]
    await _started(listener)
    try:
        acme = asyncio.create_task(feed.wait("acme"))
        beta = asyncio.create_task(feed.wait("beta"))
        await asyncio.sleep(0)
        conn.notify(changes.CHANNEL, "acme")
        done, _ = await asyncio.wait({acme, beta}, timeout=0.5)
        assert done == {acme}
        beta.cancel()
    finally:
        await listener.stop()


# ── RegistrySync ───────────────────────────────────────────────────────────


def _registry(pool: _Pool, **kw: Any) -> tuple[sync.RegistrySync, sync.Listener]:
    listener = sync.Listener(pool)  # type: ignore[arg-type]
    return sync.RegistrySync(pool, listener, interval_s=kw.get("interval_s", 3600)), listener  # type: ignore[arg-type]


async def test_apply_loads_reloads_keeps_and_forgets_as_the_table_says(
    registry: dict[str, TenantSpec],
) -> None:
    pool = _Pool()
    sync_, _ = _registry(pool)
    pool.rows["acme"] = _row("acme", "Doc")
    assert await sync_.apply("acme") is True
    loaded = registry["acme"]
    assert await sync_.apply("acme") is True
    assert registry["acme"] is loaded                 # same version: not reloaded
    pool.rows["acme"] = _row("acme", "Doc", "Tag")
    await sync_.apply("acme")
    assert registry["acme"].vertex_labels == ("Doc", "Tag")
    del pool.rows["acme"]
    assert await sync_.apply("acme") is False
    assert "acme" not in registry


async def test_reconcile_adds_changes_and_drops_until_nothing_is_left_to_do(
    registry: dict[str, TenantSpec],
) -> None:
    pool = _Pool()
    sync_, _ = _registry(pool)
    pool.rows = {"acme": _row("acme"), "beta": _row("beta"), "gamma": _row("gamma")}
    assert await sync_.reconcile() == 3
    del pool.rows["beta"]
    pool.rows["gamma"] = _row("gamma", "Doc", "Tag")
    pool.rows["delta"] = _row("delta")
    registry["stray"] = TenantSpec(name="stray", vertex_labels=("Doc",), edge_labels=())
    assert await sync_.reconcile() == 4               # beta, stray gone; gamma, delta loaded
    assert sorted(registry) == ["acme", "delta", "gamma"]
    assert registry["gamma"].vertex_labels == ("Doc", "Tag")
    assert await sync_.reconcile() == 0


async def test_reconcile_restores_a_tenant_dropped_here_even_when_unchanged(
    registry: dict[str, TenantSpec],
) -> None:
    pool = _Pool()
    sync_, _ = _registry(pool)
    pool.rows["acme"] = _row("acme")
    await sync_.reconcile()
    bootstrap.forget("acme")
    assert await sync_.reconcile() == 1
    assert "acme" in registry


async def test_ensure_restores_a_tenant_dropped_here_whose_row_did_not_change(
    registry: dict[str, TenantSpec],
) -> None:
    pool = _Pool()
    sync_, _ = _registry(pool)
    pool.rows["acme"] = _row("acme")
    await sync_.apply("acme")
    bootstrap.forget("acme")          # e.g. unregistered here, re-registered elsewhere as before
    await sync_.ensure("acme")
    assert "acme" in registry


async def test_ensure_costs_nothing_for_a_known_tenant_and_one_query_for_a_new_one(
    registry: dict[str, TenantSpec],
) -> None:
    pool = _Pool()
    sync_, _ = _registry(pool)
    pool.rows["acme"] = _row("acme")
    await sync_.ensure("acme")
    await sync_.ensure("acme")
    assert pool.queries == ["acme"]
    assert "acme" in registry


class _Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def monotonic(self) -> float:
        return self.now


async def test_a_missing_tenant_is_remembered_briefly_and_a_notification_forgets_that(
    monkeypatch: pytest.MonkeyPatch, registry: dict[str, TenantSpec],
) -> None:
    clock = _Clock()
    monkeypatch.setattr(sync, "time", clock)
    pool = _Pool()
    sync_, _ = _registry(pool)
    await sync_.ensure("ghost")
    await sync_.ensure("ghost")
    assert pool.queries == ["ghost"]                  # the second one was free
    clock.now += sync.RegistrySync.NEGATIVE_TTL_S + 0.1
    await sync_.ensure("ghost")
    assert pool.queries == ["ghost", "ghost"]         # expired: asked again

    pool.rows["ghost"] = _row("ghost")                # created on another replica
    sync_._notified("ghost")  # noqa: SLF001
    await _until(lambda: "ghost" in registry)
    await sync_.ensure("ghost")
    assert pool.queries == ["ghost"] * 3


async def test_the_memory_of_missing_tenants_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync.RegistrySync, "NEGATIVE_MAX", 3)
    pool = _Pool()
    sync_, _ = _registry(pool)
    for name in ("n1", "n2", "n3"):
        await sync_.ensure(name)
    await sync_.ensure("n1")
    assert pool.queries == ["n1", "n2", "n3"]         # all still remembered
    await sync_.ensure("n4")                          # one too many: forget them all
    await sync_.ensure("n1")
    assert pool.queries == ["n1", "n2", "n3", "n4", "n1"]


async def test_a_notification_whose_reload_fails_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture, registry: dict[str, TenantSpec],
) -> None:
    pool = _Pool()
    pool.fail_with = OSError("db down")
    sync_, _ = _registry(pool)
    sync_._notified("acme")  # noqa: SLF001
    await _until(lambda: any("tenant reload failed" in r.getMessage() for r in caplog.records))
    assert registry == {}


async def test_a_tenants_notification_through_the_listener_loads_it(
    sleeps: _FastSleep, registry: dict[str, TenantSpec],  # noqa: ARG001
) -> None:
    conn = _Conn()
    pool = _Pool(conn)
    sync_, listener = _registry(pool)
    await _started(listener)
    try:
        await _until(lambda: pool.queries == [None])   # the reconcile on connect
        pool.rows["acme"] = _row("acme")
        conn.notify(sync.TENANTS_CHANNEL, "acme")
        await _until(lambda: "acme" in registry)
        assert sync_ is not None
    finally:
        await listener.stop()


async def test_the_periodic_reconcile_survives_a_failure_and_stops_when_asked(
    sleeps: _FastSleep, caplog: pytest.LogCaptureFixture,
    registry: dict[str, TenantSpec],
) -> None:
    caplog.set_level(logging.INFO, logger="graphos.sync")
    pool = _Pool()
    pool.fail_with = OSError("db down")
    sync_, _ = _registry(pool, interval_s=7.5)
    sync_.start()
    await _until(lambda: len(pool.queries) >= 2)
    pool.fail_with = None
    pool.rows["acme"] = _row("acme")
    await _until(lambda: "acme" in registry)
    await sync_.stop()
    await sync_.stop()
    assert set(sleeps.delays) == {7.5}
    assert any("reconcile failed" in r.getMessage() for r in caplog.records)
    assert any("reconciled" in r.getMessage() for r in caplog.records)


async def test_notifications_for_one_tenant_apply_in_order(
    registry: dict[str, TenantSpec],
) -> None:
    # A tenant registered, then deleted on another server: two notifications. The
    # second reload waits for the first, so it reads the row after it and the stale
    # one cannot win (each reload in its own unordered task let it).
    pool = _Pool()
    sync_, _ = _registry(pool)
    loop = asyncio.get_running_loop()
    first, second = loop.create_future(), loop.create_future()
    pool.gates = [first, second]

    pool.rows["acme"] = _row("acme")
    sync_._notified("acme")                           # registered  # noqa: SLF001
    await _until(lambda: pool.queries == ["acme"])
    del pool.rows["acme"]
    sync_._notified("acme")                           # then deleted  # noqa: SLF001
    for _ in range(20):
        await asyncio.sleep(0)
    assert pool.queries == ["acme"]                   # the second waits its turn

    first.set_result(None)                            # the first reads the row as it is now
    await _until(lambda: pool.queries == ["acme", "acme"])
    second.set_result(None)
    await _until(lambda: not sync_._reloads)          # noqa: SLF001
    assert "acme" not in registry

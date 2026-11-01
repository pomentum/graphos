"""graphos.changes without a database: cursors, paging, retention, waking.

The tables are replaced by an in-memory fake (``_fake_feed.py``) that
answers each statement as Postgres would; ``test_changes.py`` checks the SQL
itself against a live database.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import structlog

from graphos import changes
from graphos.changes import ACTOR, START, Actor, ChangeFeed, Cursor, CursorGoneError

from ._fake_feed import FakeFeedDB

T = "acme"


def _cursors(page_changes: list[changes.Change]) -> list[str]:
    return [str(c.cursor) for c in page_changes]


@pytest.fixture
def db() -> FakeFeedDB:
    return FakeFeedDB()


# ── Cursors ────────────────────────────────────────────────────────────────


def test_a_cursor_survives_surrounding_whitespace_and_the_largest_transaction_id() -> None:
    assert Cursor.parse("  41.7\n") == Cursor(41, 7)
    biggest = Cursor(2**64 - 1, 2**63 - 1)
    assert Cursor.parse(str(biggest)) == biggest


def test_cursors_order_by_transaction_first_and_sequence_second() -> None:
    issued = ["9.9", "10.0", "10.1", "100.0", "9999.5", "10000.0"]
    shuffled = sorted(issued, key=lambda s: s[::-1])
    assert [str(c) for c in sorted(Cursor.parse(s) for s in shuffled)] == issued
    assert min(Cursor.parse(s) for s in issued) > START


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("1." + "9" * 20, id="seq-beyond-bigint"),
        pytest.param("9" * 20 + ".0", id="txid-beyond-xid8"),
        pytest.param("١٢.٣", id="non-ascii-digits"),
    ],
)
def test_a_cursor_the_feed_could_not_have_issued_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="cursor"):
        Cursor.parse(text)


# ── Reading forward ────────────────────────────────────────────────────────


async def test_paging_with_a_small_limit_returns_every_record_once_in_feed_order(
    db: FakeFeedDB,
) -> None:
    for txid, seq in [(12, 1), (9, 1), (12, 0), (10000, 0), (9999, 3), (9, 2), (11, 0)]:
        db.add(T, txid, seq)
    db.add("other", 10, 0)
    seen: list[str] = []
    after: Cursor | None = None
    pages = 0
    while True:
        page = await changes.read(db, T, after, limit=2)  # type: ignore[arg-type]
        pages += 1
        seen += _cursors(page.changes)
        after = page.next
        if not page.more:
            break
    assert seen == ["9.1", "9.2", "11.0", "12.0", "12.1", "9999.3", "10000.0"]
    assert pages == 4


async def test_a_page_exactly_full_says_more_and_the_next_one_is_empty_and_stays_put(
    db: FakeFeedDB,
) -> None:
    db.add(T, 5, 0)
    db.add(T, 6, 0)
    first = await changes.read(db, T, None, limit=2)  # type: ignore[arg-type]
    assert first.more and first.next == Cursor(6, 0)
    second = await changes.read(db, T, first.next, limit=2)  # type: ignore[arg-type]
    assert (second.changes, second.more, second.next) == ([], False, Cursor(6, 0))


async def test_records_of_still_running_transactions_wait_until_they_are_visible(
    db: FakeFeedDB,
) -> None:
    db.add(T, 5, 0)
    db.add(T, 7, 0)
    db.xmin = 6
    page = await changes.read(db, T, None, limit=10)  # type: ignore[arg-type]
    assert _cursors(page.changes) == ["5.0"]
    db.xmin = 8
    page = await changes.read(db, T, page.next, limit=10)  # type: ignore[arg-type]
    assert _cursors(page.changes) == ["7.0"]


async def test_reading_from_nowhere_starts_after_the_pruning_horizon(db: FakeFeedDB) -> None:
    for txid in (3, 4, 5, 6):
        db.add(T, txid, 0)
    db.prune_through(T, 4, 0)
    page = await changes.read(db, T, None, limit=10)  # type: ignore[arg-type]
    assert _cursors(page.changes) == ["5.0", "6.0"]
    empty = await changes.read(db, "fresh", None, limit=10)  # type: ignore[arg-type]
    assert (empty.changes, empty.next, empty.more) == ([], START, False)


async def test_a_cursor_behind_the_horizon_is_gone_and_one_at_it_is_not(db: FakeFeedDB) -> None:
    for txid in (3, 4, 5):
        db.add(T, txid, 0)
    db.prune_through(T, 4, 0)
    with pytest.raises(CursorGoneError) as gone:
        await changes.read(db, T, Cursor(3, 0), limit=10)  # type: ignore[arg-type]
    assert gone.value.horizon == Cursor(4, 0)
    assert str(gone.value) == "4.0"
    with pytest.raises(CursorGoneError):
        await changes.read(db, T, Cursor(3, 99), limit=10)  # type: ignore[arg-type]
    page = await changes.read(db, T, Cursor(4, 0), limit=10)  # type: ignore[arg-type]
    assert _cursors(page.changes) == ["5.0"]


async def test_records_decode_their_ids_and_detail_and_keep_absent_ones_absent(
    db: FakeFeedDB,
) -> None:
    db.add(T, 5, 0, ids=["a/1", "b"], detail={"where": {"k": 1}}, consumer="ci",
           trace_id="t-1", rev=3)
    db.add(T, 5, 1, op="resolve", kind="edge", label="LINKS_TO", ids=None)
    db.add(T, 5, 2, op="delete", kind="edge", label="LINKS_TO", ids=[["a", "b"]],
           src_label="Doc", dst_label="Tag", count=1)
    node, summary, edge = (await changes.read(db, T, None, 10)).changes  # type: ignore[arg-type]
    assert (node.ids, node.detail, node.consumer, node.trace_id, node.rev) == (
        ["a/1", "b"], {"where": {"k": 1}}, "ci", "t-1", 3,
    )
    assert (summary.ids, summary.detail, summary.rev) == (None, None, None)
    assert (edge.ids, edge.src_label, edge.dst_label) == ([["a", "b"]], "Doc", "Tag")


# ── Reading backwards ──────────────────────────────────────────────────────


async def test_history_pages_back_newest_first_until_older_runs_out(db: FakeFeedDB) -> None:
    for txid in range(1, 6):
        db.add(T, txid, 0)
    seen: list[str] = []
    before: Cursor | None = None
    while True:
        page = await changes.history(db, T, before, limit=2)  # type: ignore[arg-type]
        seen += _cursors(page.changes)
        if page.older is None:
            break
        assert page.more and page.older == page.changes[-1].cursor
        before = page.older
    assert seen == ["5.0", "4.0", "3.0", "2.0", "1.0"]


async def test_history_keeps_only_the_ops_asked_for_and_no_ops_means_all(db: FakeFeedDB) -> None:
    for txid, op in enumerate(["upsert", "metrics", "delete", "schema"], start=1):
        db.add(T, txid, 0, op=op)
    only = await changes.history(db, T, None, 10, ["delete", "upsert"])  # type: ignore[arg-type]
    assert [c.op for c in only.changes] == ["delete", "upsert"]
    assert (only.more, only.older) == (False, None)
    every = await changes.history(db, T, None, 10, [])  # type: ignore[arg-type]
    assert [c.op for c in every.changes] == ["schema", "delete", "metrics", "upsert"]


async def test_history_before_a_cursor_starts_just_below_it(db: FakeFeedDB) -> None:
    for seq in range(4):
        db.add(T, 7, seq)
    page = await changes.history(db, T, Cursor(7, 2), 10)  # type: ignore[arg-type]
    assert _cursors(page.changes) == ["7.1", "7.0"]


# ── Counting what was written ──────────────────────────────────────────────


async def test_written_since_sums_data_writes_only(db: FakeFeedDB) -> None:
    db.add(T, 2, 0, op="upsert", count=5)
    db.add(T, 3, 0, op="metrics", count=1000)
    db.add(T, 3, 1, op="delete", count=2)
    db.add(T, 4, 0, op="communities", count=50)
    db.add(T, 4, 1, op="resolve", count=7)
    assert await changes.written_since(db, T, Cursor(2, 0)) == (9, False)  # type: ignore[arg-type]
    assert await changes.written_since(db, T, START) == (14, False)  # type: ignore[arg-type]


async def test_written_since_reports_when_it_stopped_counting_at_the_cap(db: FakeFeedDB) -> None:
    for txid in range(1, 6):
        db.add(T, txid, 0, count=10)
    assert await changes.written_since(db, T, START, cap=3) == (30, True)  # type: ignore[arg-type]
    assert await changes.written_since(db, T, START, cap=5) == (50, True)  # type: ignore[arg-type]
    assert await changes.written_since(db, T, START, cap=6) == (50, False)  # type: ignore[arg-type]


async def test_written_since_is_unknown_once_the_records_were_pruned(db: FakeFeedDB) -> None:
    db.add(T, 9, 0)
    db.prune_through(T, 5, 0)
    assert await changes.written_since(db, T, Cursor(4, 0)) is None  # type: ignore[arg-type]
    assert await changes.written_since(db, T, Cursor(5, 0)) == (1, False)  # type: ignore[arg-type]


async def test_the_head_sits_before_every_change_not_yet_visible(db: FakeFeedDB) -> None:
    db.xmin = 2**64 - 1
    assert await changes.head(db) == Cursor(2**64 - 1, 0)  # type: ignore[arg-type]


# ── Recording ──────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _acting_as(value: Actor | None) -> Iterator[None]:
    token = ACTOR.set(value)
    try:
        yield
    finally:
        ACTOR.reset(token)


def _recorded(db: FakeFeedDB) -> dict[str, Any]:
    (sql, args), = db.executed
    assert "pg_notify" in sql
    names = ("tenant", "op", "kind", "label", "src_label", "dst_label", "ids", "rev", "count",
             "detail", "consumer", "trace_id", "channel")
    return dict(zip(names, args, strict=True))


async def test_a_record_names_its_consumer_and_the_requests_trace(db: FakeFeedDB) -> None:
    with (
        _acting_as(Actor(consumer="ci-bot")),
        structlog.contextvars.bound_contextvars(trace_id="trace-abc"),
    ):
        await changes.record(db, tenant=T, op="upsert", kind="node", count=2,  # type: ignore[arg-type]
                             label="Doc", ids=(i for i in ("a", "b")))
    got = _recorded(db)
    assert (got["consumer"], got["trace_id"], got["channel"]) == (
        "ci-bot", "trace-abc", changes.CHANNEL,
    )
    assert json.loads(got["ids"]) == ["a", "b"]
    assert got["detail"] is None


async def test_a_job_records_its_own_trace_id_over_the_log_context(db: FakeFeedDB) -> None:
    with (
        _acting_as(Actor(consumer="ops", trace_id="job:42")),
        structlog.contextvars.bound_contextvars(trace_id="ignored"),
    ):
        await changes.record(db, tenant=T, op="delete", kind="edge", count=1,  # type: ignore[arg-type]
                             ids=[["a", "b"]], detail={"edges_removed": 0})
    got = _recorded(db)
    assert (got["consumer"], got["trace_id"]) == ("ops", "job:42")
    assert (json.loads(got["ids"]), json.loads(got["detail"])) == ([["a", "b"]],
                                                                    {"edges_removed": 0})


async def test_a_write_outside_any_request_records_no_one(db: FakeFeedDB) -> None:
    with _acting_as(None), structlog.contextvars.bound_contextvars(trace_id=None):
        await changes.record(db, tenant=T, op="resolve", kind="edge", count=9)  # type: ignore[arg-type]
    got = _recorded(db)
    assert (got["consumer"], got["trace_id"], got["ids"]) == (None, None, None)


# ── Retention ──────────────────────────────────────────────────────────────


class _PrunePool:
    def __init__(self, *batches: int | BaseException) -> None:
        self.batches = list(batches)
        self.calls: list[tuple[Any, ...]] = []

    async def fetchval(self, _sql: str, *args: Any) -> int:
        self.calls.append(args)
        nxt = self.batches.pop(0) if self.batches else 0
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


async def test_pruning_runs_full_batches_until_one_comes_back_short() -> None:
    pool = _PrunePool(3, 3, 1)
    assert await changes.prune(pool, retention_h=2, batch=3) == 7  # type: ignore[arg-type]
    assert pool.calls == [(7200, 3)] * 3


async def test_pruning_nothing_is_one_query() -> None:
    pool = _PrunePool(0)
    assert await changes.prune(pool, retention_h=0.5) == 0  # type: ignore[arg-type]
    assert pool.calls == [(1800, 5000)]


# ── Waking live streams ────────────────────────────────────────────────────


class _Listener:
    def __init__(self) -> None:
        self.subscribed: dict[str, list[Callable[[str], None]]] = {}
        self.connected: list[Callable[[], object]] = []

    def subscribe(self, channel: str, callback: Callable[[str], None]) -> None:
        self.subscribed.setdefault(channel, []).append(callback)

    def on_connect(self, callback: Callable[[], object]) -> None:
        self.connected.append(callback)

    def notify(self, tenant: str) -> None:
        for cb in self.subscribed.get(changes.CHANNEL, []):
            cb(tenant)

    def reconnect(self) -> None:
        for cb in self.connected:
            cb()


def _feed(**kw: Any) -> tuple[ChangeFeed, _Listener, _PrunePool]:
    listener = _Listener()
    pool = _PrunePool()
    feed = ChangeFeed(pool, listener, retention_h=1, **{"poll_s": 5.0, **kw})  # type: ignore[arg-type]
    return feed, listener, pool


async def _soon(listener_action: Callable[[], None]) -> None:
    await asyncio.sleep(0.01)
    listener_action()


async def test_a_notification_for_the_tenant_ends_the_wait_at_once() -> None:
    feed, listener, _ = _feed()
    started = time.monotonic()
    poke = asyncio.create_task(_soon(lambda: listener.notify(T)))
    await feed.wait(T)
    await poke
    assert time.monotonic() - started < 1.0


async def test_a_notification_for_another_tenant_does_not_wake_the_stream() -> None:
    feed, listener, _ = _feed()
    started = time.monotonic()
    poke = asyncio.create_task(_soon(lambda: listener.notify("other")))
    await feed.wait(T, timeout=0.2)
    await poke
    assert time.monotonic() - started >= 0.19


async def test_without_a_notification_the_wait_ends_after_the_poll_interval() -> None:
    feed, _, _ = _feed(poll_s=0.05)
    started = time.monotonic()
    await feed.wait(T)
    assert 0.04 <= time.monotonic() - started < 1.0


async def test_a_listener_reconnect_wakes_every_stream_of_every_tenant() -> None:
    feed, listener, _ = _feed()
    waits = [asyncio.create_task(feed.wait(t)) for t in (T, T, "beta")]
    await asyncio.sleep(0.01)
    listener.reconnect()
    done, pending = await asyncio.wait(waits, timeout=1.0)
    assert len(done) == 3 and not pending


async def test_finished_waits_leave_no_waiters_behind() -> None:
    feed, listener, _ = _feed()
    waits = [asyncio.create_task(feed.wait(T)) for _ in range(3)]
    timed_out = asyncio.create_task(feed.wait("beta", timeout=0.01))
    await asyncio.sleep(0.02)
    listener.notify(T)
    await asyncio.gather(*waits, timed_out)
    assert feed._waiters == {}  # noqa: SLF001 — would otherwise grow with every stream


async def test_a_cancelled_wait_leaves_no_waiter_behind() -> None:
    feed, _, _ = _feed()
    task = asyncio.create_task(feed.wait(T))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert feed._waiters == {}  # noqa: SLF001


async def test_the_prune_loop_survives_a_failed_round_and_stops_when_asked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    feed, _, pool = _feed(prune_every_s=0.001)
    pool.batches = [RuntimeError("db down"), 4, 0]
    caplog.set_level(logging.INFO, logger="graphos.changes")
    feed.start()
    for _ in range(200):
        if len(pool.calls) >= 3:
            break
        await asyncio.sleep(0.005)
    await feed.stop()
    assert len(pool.calls) >= 3
    messages = [r.getMessage() for r in caplog.records]
    assert "change feed prune failed" in messages
    assert "change feed pruned" in messages
    calls = len(pool.calls)
    await asyncio.sleep(0.02)
    assert len(pool.calls) == calls          # stopped means stopped
    await feed.stop()                        # and stopping twice is harmless

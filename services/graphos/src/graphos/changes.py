"""The change feed: an ordered log of every write to a tenant's graph.

Every function in :mod:`graphos.writes` calls :func:`record` inside its own
transaction, so a change is logged exactly when the data commits, and a
write that rolls back leaves no record.  A record names what changed (op,
node or edge, label, ids, the new revision for a single item) and who
changed it, not the new values: consumers re-read what they need.

**Ordering that never skips.**  A record's position is ``(txid, seq)``: the
id of the transaction that wrote it and a sequence number.  A plain
sequence would lose records, because transactions commit in a different
order from the one they took their numbers in: a reader could move past 100
while 99 was still uncommitted.  So readers only see records of
transactions older than the oldest one still running (the snapshot's
``xmin``); anything that commits later has a larger txid and sorts after
every position already handed out.  The price is latency: a record becomes
visible once every write transaction that started before it has ended.

Positions follow transaction *start* order, not commit order, so two
records for the same node can appear with the later revision first.
Consumers that re-read the current state, or compare ``rev``, are
unaffected.

**Retention.**  Records older than ``GRAPHOS_CHANGES_RETENTION_H`` are
pruned in small batches.  The highest pruned position per tenant is kept as
its horizon; a cursor behind it raises :class:`CursorGoneError` (410 over
HTTP), so a consumer that fell too far behind resyncs instead of silently
missing changes.

**Who.**  The consumer is taken from :data:`ACTOR`, a context variable set
when a request is authenticated (``graphos.auth``) or a background job
starts (``graphos.jobs``); the trace id from the request's log context.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

import asyncpg
import structlog

if TYPE_CHECKING:
    from graphos.sync import Listener

log = logging.getLogger(__name__)

CHANNEL = "graphos_changes"

Op = Literal[
    "upsert", "merge", "patch", "delete", "resolve", "communities", "metrics", "schema",
]
Kind = Literal["node", "edge", "tenant"]


@dataclass(frozen=True, slots=True)
class Actor:
    consumer: str | None
    trace_id: str | None = None   # None: take the request's from the log context


ACTOR: ContextVar[Actor | None] = ContextVar("graphos_change_actor", default=None)


# ── Positions ──────────────────────────────────────────────────────────────

_CURSOR = re.compile(r"^(\d{1,20})\.(\d{1,20})$")


@dataclass(frozen=True, slots=True, order=True)
class Cursor:
    """A position in the feed; on the wire an opaque ``"<txid>.<seq>"``."""

    txid: int
    seq: int

    def __str__(self) -> str:
        return f"{self.txid}.{self.seq}"

    @classmethod
    def parse(cls, text: str) -> Cursor:
        m = _CURSOR.fullmatch(text.strip())
        if m is None:
            raise ValueError(f"not a change-feed cursor: {text!r}")
        return cls(int(m[1]), int(m[2]))


START = Cursor(0, 0)


class CursorGoneError(Exception):
    """The records after this cursor have been pruned."""

    def __init__(self, horizon: Cursor) -> None:
        super().__init__(str(horizon))
        self.horizon = horizon


# ── Writing ────────────────────────────────────────────────────────────────


async def record(  # noqa: PLR0913 — one record's fields
    conn: asyncpg.Connection,
    *,
    tenant: str,
    op: Op,
    kind: Kind,
    count: int,
    label: str | None = None,
    ids: Sequence[Any] | None = None,
    src_label: str | None = None,
    dst_label: str | None = None,
    rev: int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Log one change in the caller's open transaction.

    ``ids`` are external ids for nodes, ``[src_id, dst_id]`` pairs for edges,
    or ``None`` when the change is too broad to list (``resolve``,
    ``communities``).  The ``NOTIFY`` that wakes live streams is delivered
    on commit only.
    """
    actor = ACTOR.get()
    trace_id = actor.trace_id if actor and actor.trace_id else (
        structlog.contextvars.get_contextvars().get("trace_id")
    )
    # One round trip: the insert and the notification in one statement.
    await conn.execute(
        "WITH logged AS ("
        "  INSERT INTO graphos_meta.changes "
        "  (tenant, op, kind, label, src_label, dst_label, ids, rev, count, detail, "
        "   consumer, trace_id) "
        "  VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10::jsonb, $11, $12) "
        "  RETURNING tenant"
        ") SELECT pg_notify($13, tenant) FROM logged;",
        tenant, op, kind, label, src_label, dst_label,
        None if ids is None else json.dumps(list(ids)),
        rev, count,
        None if detail is None else json.dumps(detail),
        actor.consumer if actor else None,
        trace_id,
        CHANNEL,
    )


# ── Reading ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Change:
    cursor: Cursor
    at: datetime
    op: str
    kind: str
    label: str | None
    src_label: str | None
    dst_label: str | None
    ids: list[Any] | None
    rev: int | None
    count: int
    detail: dict[str, Any] | None
    consumer: str | None
    trace_id: str | None


@dataclass(frozen=True, slots=True)
class Page:
    changes: list[Change]
    next: Cursor       # pass as ``after`` to continue
    more: bool         # the page was full: more may be ready now


# Columns are qualified throughout: an ORDER BY of a bare ``txid`` would sort
# by the text output column, putting "10000" before "9999", and the pages
# would then skip records whenever transaction ids cross a power of ten.
_READ_SQL = """
SELECT c.txid::text AS txid_text, c.seq, c.at, c.op, c.kind, c.label,
       c.src_label, c.dst_label, c.ids::text AS ids_text, c.rev, c.count,
       c.detail::text AS detail_text, c.consumer, c.trace_id
FROM graphos_meta.changes c
WHERE c.tenant = $1
  AND (c.txid, c.seq) > ($2::text::xid8, $3::bigint)
  AND c.txid < pg_snapshot_xmin(pg_current_snapshot())
ORDER BY c.txid, c.seq
LIMIT $4;
"""


async def horizon(conn: asyncpg.Connection, tenant: str) -> Cursor:
    """The last pruned position of ``tenant`` (``START`` if none)."""
    row = await conn.fetchrow(
        "SELECT txid::text AS txid_text, seq FROM graphos_meta.change_horizon WHERE tenant = $1;",
        tenant,
    )
    return START if row is None else Cursor(int(row["txid_text"]), int(row["seq"]))


async def read(
    conn: asyncpg.Connection, tenant: str, after: Cursor | None, limit: int,
) -> Page:
    """The visible records after ``after`` (from the oldest kept when None).

    Raises:
        CursorGoneError: records after ``after`` were pruned.
    """
    edge = await horizon(conn, tenant)
    if after is None:
        after = edge
    elif after < edge:
        raise CursorGoneError(edge)
    rows = await conn.fetch(_READ_SQL, tenant, str(after.txid), after.seq, limit)
    changes = [_change(r) for r in rows]
    return Page(changes, next=changes[-1].cursor if changes else after, more=len(rows) == limit)


def _change(r: asyncpg.Record) -> Change:
    return Change(
        cursor=Cursor(int(r["txid_text"]), int(r["seq"])),
        at=r["at"], op=r["op"], kind=r["kind"], label=r["label"],
        src_label=r["src_label"], dst_label=r["dst_label"],
        ids=None if r["ids_text"] is None else json.loads(r["ids_text"]),
        rev=r["rev"], count=r["count"],
        detail=None if r["detail_text"] is None else json.loads(r["detail_text"]),
        consumer=r["consumer"], trace_id=r["trace_id"],
    )


_HISTORY_SQL = """
SELECT c.txid::text AS txid_text, c.seq, c.at, c.op, c.kind, c.label,
       c.src_label, c.dst_label, c.ids::text AS ids_text, c.rev, c.count,
       c.detail::text AS detail_text, c.consumer, c.trace_id
FROM graphos_meta.changes c
WHERE c.tenant = $1
  AND ($2::text IS NULL OR (c.txid, c.seq) < ($2::text::xid8, $3::bigint))
  AND c.txid < pg_snapshot_xmin(pg_current_snapshot())
  AND ($5::text[] IS NULL OR c.op = ANY($5::text[]))
ORDER BY c.txid DESC, c.seq DESC
LIMIT $4;
"""


@dataclass(frozen=True, slots=True)
class HistoryPage:
    changes: list[Change]      # newest first
    older: Cursor | None       # pass as ``before`` for the next page; None at the end
    more: bool


async def history(
    conn: asyncpg.Connection,
    tenant: str,
    before: Cursor | None,
    limit: int,
    ops: Sequence[str] | None = None,
) -> HistoryPage:
    """The visible records before ``before`` (the newest when None), newest
    first: the past of the feed, for people reading it.  Pruned records are
    simply absent."""
    rows = await conn.fetch(
        _HISTORY_SQL, tenant,
        None if before is None else str(before.txid), 0 if before is None else before.seq,
        limit, list(ops) if ops else None,
    )
    changes = [_change(r) for r in rows]
    more = len(rows) == limit
    return HistoryPage(changes, older=changes[-1].cursor if changes and more else None, more=more)


async def head(conn: asyncpg.Connection) -> Cursor:
    """A position before every change not yet visible.

    For a full resync: take the head, then read the data, then follow the
    feed from the head.  Changes the read already saw may be delivered
    again (at least once), never skipped.
    """
    xmin = await conn.fetchval("SELECT pg_snapshot_xmin(pg_current_snapshot())::text;")
    return Cursor(int(xmin), 0)


# ── Retention ──────────────────────────────────────────────────────────────

# One batch: delete the oldest expired records of finished transactions,
# then raise each tenant's horizon to the highest position deleted.  Only
# records with txid < xmin are eligible: a transaction still running when
# its older neighbours are pruned must not end up behind a horizon.
_PRUNE_SQL = """
WITH gone AS (
    DELETE FROM graphos_meta.changes
    WHERE ctid = ANY(ARRAY(
        SELECT ctid FROM graphos_meta.changes
        WHERE at < now() - make_interval(secs => $1)
          AND txid < pg_snapshot_xmin(pg_current_snapshot())
        ORDER BY at
        LIMIT $2
        FOR UPDATE SKIP LOCKED))
    RETURNING tenant, txid, seq
), last AS (
    SELECT DISTINCT ON (tenant) tenant, txid, seq
    FROM gone ORDER BY tenant, txid DESC, seq DESC
), raised AS (
    INSERT INTO graphos_meta.change_horizon AS h (tenant, txid, seq)
    SELECT tenant, txid, seq FROM last
    ON CONFLICT (tenant) DO UPDATE SET txid = EXCLUDED.txid, seq = EXCLUDED.seq
    WHERE (h.txid, h.seq) < (EXCLUDED.txid, EXCLUDED.seq)
    RETURNING 1
)
SELECT count(*) FROM gone;
"""


async def prune(pool: asyncpg.Pool, retention_h: float, *, batch: int = 5000) -> int:
    """Delete expired records, ``batch`` per transaction.  Returns how many.

    Small batches keep each pruning transaction short: a long one would
    itself hold back every tenant's feed (see the module docstring).
    """
    total = 0
    while True:
        n = await pool.fetchval(_PRUNE_SQL, retention_h * 3600, batch)
        total += n
        if n < batch:
            return total


# ── Waking live streams ────────────────────────────────────────────────────


class ChangeFeed:
    """Per-process service behind the live streams.

    Wakes the streams of the tenant named in each :data:`CHANNEL`
    notification (received through the process's shared
    :class:`graphos.sync.Listener`), and prunes expired records.  A
    notification only shortens the wait: streams poll every ``poll_s``
    anyway, so a lost notification or listener connection delays a change,
    never loses it.
    """

    def __init__(
        self, pool: asyncpg.Pool, listener: Listener, *, retention_h: float, poll_s: float,
        prune_every_s: float = 60.0,
    ) -> None:
        self._pool = pool
        self._retention_h = retention_h
        self.poll_s = poll_s
        self._prune_every_s = prune_every_s
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self._tasks: list[asyncio.Task[None]] = []
        listener.subscribe(CHANNEL, self._notified)
        listener.on_connect(self._wake_all)   # anything missed while not listening

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._prune_loop(), name="graphos-changes-prune"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def wait(self, tenant: str, timeout: float | None = None) -> None:
        """Until a change of ``tenant`` commits, or ``timeout`` (default ``poll_s``)."""
        event = asyncio.Event()
        self._waiters.setdefault(tenant, set()).add(event)
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), self.poll_s if timeout is None else timeout)
        finally:
            waiters = self._waiters.get(tenant)
            if waiters is not None:
                waiters.discard(event)
                if not waiters:
                    del self._waiters[tenant]

    def _notified(self, tenant: str) -> None:
        for event in self._waiters.get(tenant, ()):
            event.set()

    def _wake_all(self) -> None:
        for waiters in self._waiters.values():
            for event in waiters:
                event.set()

    async def _prune_loop(self) -> None:
        while True:
            try:
                n = await prune(self._pool, self._retention_h)
                if n:
                    log.info("change feed pruned", extra={"records": n})
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — try again next round
                log.warning("change feed prune failed", exc_info=True)
            await asyncio.sleep(self._prune_every_s)

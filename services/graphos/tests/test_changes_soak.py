"""The change feed under concurrent load: nothing lost, nothing repeated.

DB-gated.  For ``GRAPHOS_SOAK_S`` seconds (default 6), writers run random
writes, some in deliberately slow transactions (so commits land out of start
order) and some rolled back, while readers follow the feed: polling readers
with random page sizes and pauses, and an SSE reader over real HTTP that
disconnects and resumes with ``Last-Event-ID`` every few events.

Afterwards every reader must hold exactly the committed records, in feed
order: no gap, no duplicate, nothing from a rolled-back transaction; and the
records must match what the writers say they committed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import time
from collections import Counter
from uuid import uuid4

import httpx
import pytest

from graphos import changes, writes
from graphos.bootstrap import TenantSpec, register_tenant

from ._live import Stack, stack
from .conftest import requires_db

SOAK_S = float(os.environ.get("GRAPHOS_SOAK_S", "6"))
WRITERS = 6
POLLERS = 3
IDS = [f"n{i}" for i in range(40)]


class Tally:
    def __init__(self) -> None:
        self.ops: Counter[str] = Counter()
        self.rolled_back = 0
        self.edges: list[tuple[str, str]] = []   # written, maybe deleted since


async def _writer(s: Stack, tenant: str, rng: random.Random, deadline: float, tally: Tally) -> None:
    while time.monotonic() < deadline:
        roll = rng.random()
        if roll < 0.25:
            batch = rng.sample(IDS, rng.randint(1, 5))
            await writes.upsert_nodes(
                s.pool, tenant=tenant, label="Doc",
                rows=[{"id": i, "props": {"v": rng.random()}} for i in batch],
            )
            tally.ops["upsert/node"] += 1
        elif roll < 0.40:
            if await writes.patch_node(
                s.pool, tenant=tenant, external_id=rng.choice(IDS), patch={"p": rng.random()},
            ):
                tally.ops["patch/node"] += 1
        elif roll < 0.55:
            pair = (rng.choice(IDS), rng.choice(IDS))
            written, _ = await writes.upsert_edges(
                s.pool, tenant=tenant, src_label="Doc", dst_label="Doc", edge_type="LINKS",
                rows=[{"src_id": pair[0], "dst_id": pair[1]}],
            )
            if written:
                tally.ops["upsert/edge"] += 1
                tally.edges.append(pair)
        elif roll < 0.65:
            # Usually an edge that exists; sometimes one already deleted.
            a, b = rng.choice(tally.edges) if tally.edges else (IDS[0], IDS[1])
            if await writes.delete_edge(s.pool, tenant=tenant, edge_type="LINKS",
                                        src_id=a, dst_id=b):
                tally.ops["delete/edge"] += 1
        elif roll < 0.85:
            # A slow transaction: it takes its txid now and commits later,
            # after transactions that started after it.
            async with s.pool.acquire() as conn, conn.transaction():
                await changes.record(conn, tenant=tenant, op="merge", kind="node",
                                     ids=["slow"], count=1)
                await asyncio.sleep(rng.uniform(0.05, 0.8))
            tally.ops["merge/node"] += 1
        else:
            async with s.pool.acquire() as conn:
                tx = conn.transaction()
                await tx.start()
                await changes.record(conn, tenant=tenant, op="merge", kind="node",
                                     ids=["rolled-back"], count=1)
                await asyncio.sleep(rng.uniform(0, 0.2))
                await tx.rollback()
            tally.rolled_back += 1
        await asyncio.sleep(rng.uniform(0, 0.02))


async def _poller(s: Stack, tenant: str, rng: random.Random, done: asyncio.Event) -> list[str]:
    seen: list[str] = []
    after = None
    quiet = 0
    while True:
        async with s.pool.acquire() as conn:
            page = await changes.read(conn, tenant, after, rng.randint(1, 50))
        seen += [str(c.cursor) for c in page.changes]
        after = page.next
        if done.is_set() and not page.changes:
            quiet += 1
            if quiet >= 3:
                return seen
            await asyncio.sleep(0.2)
        else:
            quiet = 0
            await asyncio.sleep(rng.uniform(0, 0.05))


async def _sse_reader(s: Stack, tenant: str, start: str, rng: random.Random,
                      done: asyncio.Event) -> tuple[list[str], int]:
    """Follows the stream, hanging up every few events and resuming with
    ``Last-Event-ID``.  Lines go through a queue so that waiting for one can
    time out without cancelling a read inside httpx."""
    seen: list[str] = []
    reconnects = 0
    last = start

    async def pump(resp: httpx.Response, queue: asyncio.Queue[str | None]) -> None:
        async for line in resp.aiter_lines():
            await queue.put(line)
        await queue.put(None)

    async with httpx.AsyncClient(base_url=s.url, timeout=httpx.Timeout(30, read=None)) as client:
        while True:
            budget = rng.randint(3, 25)           # events before hanging up
            queue: asyncio.Queue[str | None] = asyncio.Queue()
            async with client.stream(
                "GET", f"/v1/{tenant}/changes/stream", headers={"Last-Event-ID": last},
            ) as resp:
                assert resp.status_code == 200
                pumping = asyncio.create_task(pump(resp, queue))
                try:
                    event: dict[str, str] = {}
                    idle_since = time.monotonic()
                    while budget > 0:
                        try:
                            line = await asyncio.wait_for(queue.get(), timeout=0.5)
                        except TimeoutError:
                            if done.is_set() and time.monotonic() - idle_since > 2.0:
                                return seen, reconnects
                            continue
                        assert line is not None, "the server ended the stream"
                        if line == "" and event:
                            if event.get("event") == "change":
                                assert json.loads(event["data"])["cursor"] == event["id"]
                                seen.append(event["id"])
                                last = event["id"]
                                budget -= 1
                                idle_since = time.monotonic()
                            event = {}
                        elif line and not line.startswith(":"):
                            field, _, value = line.partition(":")
                            event[field] = value.lstrip(" ")
                finally:
                    pumping.cancel()
                    with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                        await pumping
            reconnects += 1


@requires_db
@pytest.mark.asyncio
async def test_no_change_is_lost_or_repeated_under_concurrency() -> None:
    rng = random.Random(int(os.environ.get("GRAPHOS_SOAK_SEED", "20261006")))
    async with stack(poll_s=0.5) as s:
        tenant = f"soak_{uuid4().hex[:10]}"
        await register_tenant(s.pool, TenantSpec(
            name=tenant, vertex_labels=("Doc",), edge_labels=("LINKS",),
        ))
        await writes.upsert_nodes(s.pool, tenant=tenant, label="Doc",
                                  rows=[{"id": i, "props": {}} for i in IDS])
        async with s.pool.acquire() as conn:
            start = str((await changes.read(conn, tenant, None, 10)).next)
        tally = Tally()
        done = asyncio.Event()
        deadline = time.monotonic() + SOAK_S

        pollers = [asyncio.create_task(_poller(s, tenant, random.Random(rng.random()), done))
                   for _ in range(POLLERS)]
        sse = asyncio.create_task(_sse_reader(s, tenant, start, random.Random(rng.random()), done))
        await asyncio.gather(*(
            _writer(s, tenant, random.Random(rng.random()), deadline, tally)
            for _ in range(WRITERS)
        ))
        done.set()
        polled = await asyncio.gather(*pollers)
        streamed, reconnects = await asyncio.wait_for(sse, timeout=60)

        async with s.pool.acquire() as conn:
            truth = []
            after = None
            while True:
                page = await changes.read(conn, tenant, after, 1000)
                truth += page.changes
                after = page.next
                if not page.more:
                    break
        raw = await s.pool.fetch(
            "SELECT txid::text AS txid_text, seq, op, count, at FROM graphos_meta.changes "
            "WHERE tenant = $1 ORDER BY txid, seq LIMIT 6;", tenant)
        raw_initial = await s.pool.fetch(
            "SELECT txid::text AS txid_text, seq, at FROM graphos_meta.changes "
            "WHERE tenant = $1 AND op = 'upsert' AND count = $2;", tenant, len(IDS))
        await s.pool.execute("DELETE FROM graphos_meta.changes WHERE tenant = $1;", tenant)

    cursors = [str(c.cursor) for c in truth]
    # On failure, enough to tell "never written" from "written but not
    # returned": the first records read with and without the visibility rule.
    why = json.dumps({
        "tenant": tenant, "start": start,
        "feed_first": [(str(c.cursor), c.op, c.count) for c in truth[:5]],
        "table_first": [(r["txid_text"], r["seq"], r["op"], r["count"]) for r in raw],
        "table_initial": [(r["txid_text"], r["seq"]) for r in raw_initial],
        "pollers_first": [p[:5] for p in polled], "sse_first": streamed[:5],
    })
    # The feed itself: strictly increasing, one record per committed write.
    assert [c.cursor for c in truth] == sorted({c.cursor for c in truth}), why
    assert (truth[0].op, truth[0].count) == ("upsert", len(IDS)), why
    body = truth[1:]                                  # after the initial upsert
    assert Counter(f"{c.op}/{c.kind}" for c in body) == tally.ops, why
    assert not any(c.ids == ["rolled-back"] for c in truth)
    assert tally.rolled_back > 0
    assert all(tally.ops[k] > 0 for k in (
        "upsert/node", "patch/node", "upsert/edge", "delete/edge", "merge/node",
    ))

    # Every reader: exactly those records, in that order.
    for i, seen in enumerate(polled):
        assert seen == cursors, f"poller {i}: {len(seen)} vs {len(cursors)}; {why}"
    assert streamed == cursors[1:], f"sse: {len(streamed)} vs {len(cursors) - 1}; {why}"
    assert reconnects > 3
    print(
        f"\nsoak {SOAK_S:.0f}s: {len(cursors)} records, {sum(tally.ops.values())} writes, "
        f"{tally.rolled_back} rolled back, {reconnects} SSE reconnects; ops {dict(tally.ops)}",
    )

"""The live change stream (Server-Sent Events) over real HTTP.

DB-gated.  Each test runs uvicorn in-process (``_live.py``) with its own
pool and, unless it tests the fallback, the change feed's listener.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
import pytest

from graphos import changes, writes
from graphos.bootstrap import TenantSpec, register_tenant

from ._live import Stack, stack
from .conftest import requires_db


class Events:
    """Reads one SSE response: events as dicts, comments as ``{"comment": …}``."""

    def __init__(self, response: httpx.Response) -> None:
        self._lines = response.aiter_lines()

    async def next(self, timeout: float = 5.0) -> dict[str, Any]:
        event: dict[str, Any] = {}
        async with asyncio.timeout(timeout):
            async for line in self._lines:
                if line == "":
                    if event:
                        return event
                    continue
                if line.startswith(":"):
                    return {"comment": line[1:].strip()}
                field, _, value = line.partition(":")
                event[field] = value[1:] if value.startswith(" ") else value
        raise EOFError("stream ended")

    async def changes(self, n: int, timeout: float = 5.0) -> list[dict[str, Any]]:
        out = []
        deadline = time.monotonic() + timeout
        while len(out) < n:
            event = await self.next(max(0.01, deadline - time.monotonic()))
            if event.get("event") == "change":
                assert event["id"] == json.loads(event["data"])["cursor"]
                out.append(json.loads(event["data"]))
        return out


@asynccontextmanager
async def _open(
    s: Stack, tenant: str, **headers_or_params: str,
) -> AsyncIterator[tuple[httpx.Response, Events]]:
    params = {k: v for k, v in headers_or_params.items() if k == "after"}
    headers = {"Last-Event-ID": headers_or_params["last_event_id"]} \
        if "last_event_id" in headers_or_params else {}
    async with httpx.AsyncClient(base_url=s.url, timeout=httpx.Timeout(30, read=None)) as client, \
            client.stream("GET", f"/v1/{tenant}/changes/stream", params=params,
                          headers=headers) as resp:
        yield resp, Events(resp)


async def _tenant(s: Stack) -> str:
    tenant = f"chgs_{uuid4().hex[:10]}"
    await register_tenant(s.pool, TenantSpec(name=tenant, vertex_labels=("Doc",), edge_labels=()))
    return tenant


async def _write(s: Stack, tenant: str, *ids: str) -> None:
    for i in ids:
        await writes.upsert_nodes(s.pool, tenant=tenant, label="Doc", rows=[{"id": i, "props": {}}])


async def _head(s: Stack) -> str:
    async with s.pool.acquire() as conn:
        return str(await changes.head(conn))


@requires_db
@pytest.mark.asyncio
async def test_stream_delivers_every_write_in_order() -> None:
    async with stack() as s:
        t = await _tenant(s)
        async with _open(s, t, after=await _head(s)) as (resp, events):
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            assert resp.headers["cache-control"] == "no-cache"
            assert (await events.next()) == {"retry": "2000"}
            await _write(s, t, "a", "b", "c")
            got = await events.changes(3)
        assert [c["ids"] for c in got] == [["a"], ["b"], ["c"]]
        async with httpx.AsyncClient(base_url=s.url) as client:
            listed = (await client.get(f"/v1/{t}/changes")).json()["changes"]
        assert got == listed[-3:]          # the same records as the REST pages


@requires_db
@pytest.mark.asyncio
async def test_a_notification_wakes_the_stream_at_once() -> None:
    async with stack(poll_s=30) as s:     # without the notification: 30 s
        t = await _tenant(s)
        async with _open(s, t, after=await _head(s)) as (_resp, events):
            await events.next()           # retry
            await asyncio.sleep(0.3)      # the stream is now waiting
            started = time.monotonic()
            await _write(s, t, "a")
            await events.changes(1, timeout=5)
            assert time.monotonic() - started < 3


@requires_db
@pytest.mark.asyncio
async def test_without_the_listener_the_stream_still_polls() -> None:
    async with stack(feed=False, poll_s=0.2) as s:
        t = await _tenant(s)
        async with _open(s, t, after=await _head(s)) as (_resp, events):
            await _write(s, t, "a", "b")
            assert [c["ids"] for c in await events.changes(2)] == [["a"], ["b"]]


@requires_db
@pytest.mark.asyncio
async def test_reconnecting_with_last_event_id_neither_repeats_nor_skips() -> None:
    async with stack() as s:
        t = await _tenant(s)
        head = await _head(s)
        async with _open(s, t, after=head) as (_resp, events):
            await _write(s, t, "a", "b")
            first = await events.changes(2)
        await _write(s, t, "c", "d")       # while disconnected
        async with _open(s, t, after=head, last_event_id=first[-1]["cursor"]) as (_r, events):
            await _write(s, t, "e")
            second = await events.changes(3)
            with pytest.raises(TimeoutError):
                await events.changes(1, timeout=1.5)     # nothing more, no repeats
        assert [c["ids"][0] for c in first + second] == ["a", "b", "c", "d", "e"]


@requires_db
@pytest.mark.asyncio
async def test_an_idle_stream_sends_keep_alives() -> None:
    async with stack(poll_s=0.1, changes_heartbeat_s=0.3) as s:
        t = await _tenant(s)
        async with _open(s, t, after=await _head(s)) as (_resp, events):
            await events.next()
            assert await events.next(timeout=3) == {"comment": "keep-alive"}


@requires_db
@pytest.mark.asyncio
async def test_a_pruned_position_is_410_and_falling_behind_ends_with_gone() -> None:
    async with stack(poll_s=0.1) as s:
        t = await _tenant(s)
        await _write(s, t, "a")
        await s.pool.execute(
            "INSERT INTO graphos_meta.change_horizon (tenant, txid, seq) "
            "VALUES ($1, '5'::xid8, 5);", t,
        )
        async with httpx.AsyncClient(base_url=s.url) as client:
            gone = await client.get(f"/v1/{t}/changes/stream", params={"after": "1.1"})
            assert gone.status_code == 410

        async with _open(s, t, after=await _head(s)) as (_resp, events):
            await events.next()
            # Retention overtakes the stream: its next read is behind the horizon.
            await s.pool.execute(
                "UPDATE graphos_meta.change_horizon SET txid = '9999999999'::xid8 "
                "WHERE tenant = $1;", t,
            )
            await _write(s, t, "b")
            event = await events.next(timeout=5)
            while "comment" in event:
                event = await events.next(timeout=5)
            assert event["event"] == "gone"
            assert json.loads(event["data"])["status"] == 410
            with pytest.raises(EOFError):
                await events.next(timeout=5)


@requires_db
@pytest.mark.asyncio
async def test_closed_streams_release_their_connections() -> None:
    async with stack(poll_s=0.1) as s:
        t = await _tenant(s)
        for _ in range(10):
            async with _open(s, t) as (_resp, events):
                await events.next()
        await asyncio.sleep(0.5)
        # Only the listener holds a connection between requests.
        assert s.pool.get_size() - s.pool.get_idle_size() == 1
        async with httpx.AsyncClient(base_url=s.url) as client:
            assert (await client.get("/healthz")).status_code == 200

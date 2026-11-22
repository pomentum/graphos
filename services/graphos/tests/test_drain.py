"""The drain on SIGTERM (graphos.drain, ADR-0015), over real HTTP.

No database: the app runs with its lifespan off, behind its real middleware
stack, in uvicorn in-process.  The server is :class:`graphos.__main__.GraphosServer`
and the signal goes through its ``handle_exit``, as a real one would.
"""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Iterator, Mapping
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from pydantic import ValidationError

from graphos import changes, console, db
from graphos import drain as drain_module
from graphos.__main__ import GraphosServer
from graphos.api import changes as changes_api
from graphos.app import create_app
from graphos.config import Settings, drain_reserve_s
from graphos.drain import Drain, DrainHooks
from graphos.observability.metrics import DRAINING

from ._live import graphos_server


@pytest.fixture(autouse=True)
def _keep_the_process_drain() -> Iterator[None]:
    """A drained app's drain must not stay the process's for later tests."""
    before = drain_module.current()
    yield
    drain_module.install(before)
    DRAINING.set(0)


def _app(**settings: Any) -> FastAPI:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True, **settings))

    async def slow(s: float = 0.0) -> dict[str, float]:
        await asyncio.sleep(s)
        return {"slept": s}

    app.add_api_route("/test/slow", slow)
    return app


async def _get(url: str, path: str) -> httpx.Response:
    async with httpx.AsyncClient(base_url=url, timeout=60) as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_a_drain_refuses_new_requests_and_finishes_the_running_ones() -> None:
    app = _app(shutdown_grace_s=4, drain_delay_s=0.5)   # reserve 1 s: the cut at 3 s
    drain: Drain = app.state.drain
    async with graphos_server(app) as (url, server, task):
        short = asyncio.create_task(_get(url, "/test/slow?s=1.5"))
        long = asyncio.create_task(_get(url, "/test/slow?s=30"))
        await asyncio.sleep(0.2)
        started = time.monotonic()
        server.handle_exit(signal.SIGTERM, None)
        await asyncio.sleep(0.05)

        ready = await _get(url, "/readyz")
        assert ready.status_code == 503
        assert ready.json() == {"status": "not_ready", "reason": "draining"}
        # During the delay requests are still served, and their connection closed.
        served = await _get(url, "/test/slow")
        assert served.status_code == 200
        assert served.headers["connection"] == "close"

        await asyncio.sleep(0.7)
        refused = await _get(url, "/test/slow")
        assert refused.status_code == 503
        assert refused.headers["retry-after"] == "2"
        assert refused.headers["connection"] == "close"
        assert refused.headers["content-type"] == "application/problem+json"
        assert refused.json()["code"] == "draining"
        assert (await _get(url, "/healthz")).status_code == 200
        assert (await _get(url, "/readyz")).status_code == 503
        metrics = await _get(url, "/metrics")
        assert metrics.status_code == 200
        assert "graphos_draining 1.0" in metrics.text
        assert "graphos_drain_refused_total" in metrics.text

        # The running request ends in time; the one that outlasts the drain is cut
        # behind the app's BaseHTTPMiddleware stack, and answers 503, not 500.
        assert (await short).status_code == 200
        cut = await long
        assert cut.status_code == 503
        assert cut.json()["code"] == "draining"
        assert 2.5 < time.monotonic() - started < 4

        await asyncio.wait_for(task, timeout=10)
    assert time.monotonic() - started < 4.5
    assert drain.exit_code == 0
    assert drain.phase == "done"
    # The signal is not raised again after the stop (uvicorn would).
    assert server._captured_signals == []


@pytest.mark.asyncio
async def test_a_second_signal_cuts_the_drain_short() -> None:
    app = _app(shutdown_grace_s=30, drain_delay_s=1)
    drain: Drain = app.state.drain
    async with graphos_server(app) as (url, server, task):
        long = asyncio.create_task(_get(url, "/test/slow?s=60"))
        await asyncio.sleep(0.2)
        started = time.monotonic()
        server.handle_exit(signal.SIGTERM, None)
        await asyncio.sleep(0.2)
        server.handle_exit(signal.SIGINT, None)
        cut = await long
        assert cut.status_code == 503
        await asyncio.wait_for(task, timeout=10)
    assert time.monotonic() - started < 3
    assert drain.exit_code == 0


@pytest.mark.asyncio
async def test_with_nothing_running_the_drain_ends_after_its_delay() -> None:
    app = _app(shutdown_grace_s=20, drain_delay_s=0.3)
    async with graphos_server(app) as (_url, server, task):
        started = time.monotonic()
        server.handle_exit(signal.SIGTERM, None)
        await asyncio.wait_for(task, timeout=10)
    assert time.monotonic() - started < 2


@pytest.mark.asyncio
async def test_the_drain_asks_the_jobs_and_streams_in_order() -> None:
    calls: list[str] = []
    running = [1]

    async def begin() -> None:
        calls.append("begin")

    def refuse() -> None:
        calls.append("refuse")

    async def cut() -> Mapping[str, int]:
        calls.append("cut")
        running[0] = 0
        return {"queued": 1}

    drain = Drain(grace_s=2, delay_s=0.2)   # reserve 0.5 s: the cut at 1.5 s
    drain.hooks = DrainHooks(begin=begin, refuse=refuse, cut=cut, running_jobs=lambda: running[0])
    left: list[float] = []
    drain.on_done = left.append
    started = time.monotonic()
    drain.signal("SIGTERM")
    assert drain.draining and not drain.refusing
    await drain.wait()
    took = time.monotonic() - started
    assert calls == ["begin", "refuse", "cut"]
    assert 1.4 < took < 2           # a running job is waited for until the cut
    assert drain.exit_code == 0
    assert len(left) == 1 and 0 < left[0] <= 0.6


@pytest.mark.asyncio
async def test_a_job_that_could_not_be_handed_back_makes_the_exit_status_1() -> None:
    async def cut() -> Mapping[str, int]:
        return {"error": 1}

    drain = Drain(grace_s=2, delay_s=0)
    drain.hooks = DrainHooks(cut=cut)
    drain.signal("SIGTERM")
    await drain.wait()
    assert drain.exit_code == 1


@pytest.mark.asyncio
async def test_a_failing_step_does_not_stop_the_steps_after_it() -> None:
    calls: list[str] = []

    async def begin() -> None:
        raise RuntimeError("database gone")

    async def cut() -> Mapping[str, int]:
        calls.append("cut")
        return {}

    drain = Drain(grace_s=2, delay_s=0)
    drain.hooks = DrainHooks(begin=begin, refuse=lambda: calls.append("refuse"), cut=cut)
    drain.signal("SIGTERM")
    await drain.wait()
    assert calls == ["refuse", "cut"]
    assert drain.exit_code == 1


def test_the_reserve_is_a_quarter_of_the_grace_at_most_5_s() -> None:
    assert drain_reserve_s(8) == 2
    assert drain_reserve_s(30) == 5


def test_a_delay_that_leaves_no_time_to_finish_is_refused() -> None:
    Settings(db_password="x", shutdown_grace_s=8, drain_delay_s=5.9)
    with pytest.raises(ValidationError, match="GRAPHOS_DRAIN_DELAY_S"):
        Settings(db_password="x", shutdown_grace_s=8, drain_delay_s=6)
    with pytest.raises(ValidationError):
        Settings(db_password="x", shutdown_grace_s=1)


def test_the_default_grace_fits_docker_and_kubernetes() -> None:
    settings = Settings(db_password="x")
    assert settings.shutdown_grace_s == 8
    assert settings.drain_delay_s == 0


@pytest.mark.asyncio
async def test_every_graphos_connection_ends_a_killed_process_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PostgreSQL does not notice a vanished client while its statement runs; the
    # check makes it end the statement and release its locks (ADR-0015, E4).
    made: list[dict[str, str]] = []

    async def create_pool(**kwargs: Any) -> object:
        made.append(kwargs["server_settings"])
        return object()

    monkeypatch.setattr(db.asyncpg, "create_pool", create_pool)
    settings = Settings(db_password="x", cypher_temp_file_limit="-1")
    await db.create_pool(settings)
    await console.start(settings, owner=None)  # type: ignore[arg-type]
    assert [s["client_connection_check_interval"] for s in made] == ["5000", "5000"]


@pytest.mark.asyncio
async def test_the_first_signal_arms_a_watchdog_from_the_event_loop() -> None:
    app = _app(shutdown_grace_s=20, drain_delay_s=0.1)
    server = GraphosServer(uvicorn.Config(
        app, host="127.0.0.1", port=0, lifespan="off", log_level="warning",
    ))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        server.handle_exit(signal.SIGTERM, None)
        assert server._watchdog is None          # not from the signal handler itself
        await asyncio.sleep(0.05)
        assert server._watchdog is not None and server._watchdog.is_alive()
        await asyncio.wait_for(task, timeout=10)
    finally:
        server.disarm_watchdog()
    assert server._watchdog is None


@pytest.mark.asyncio
async def test_a_change_feed_stream_ends_with_a_draining_event_when_refusing() -> None:
    drain = Drain(grace_s=8, delay_s=0)
    drain.phase = "refusing"
    state = SimpleNamespace(
        settings=Settings(db_password="x"), pool=None, change_feed=None, drain=drain,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    page = changes.Page(changes=[], next=changes.Cursor(txid=1, seq=0), more=False)
    events = [event async for event in changes_api._events(request, "acme", page)]  # type: ignore[arg-type]  # a stand-in request
    assert events == ["retry: 2000\n\n", 'event: draining\ndata: {"reason": "draining"}\n\n']
    assert drain.streams_ended == 1

"""The drain on SIGTERM against the database (graphos.drain, graphos.jobs; ADR-0015).

DB-gated.  Job runners on the test database for the hand-back and the sweep;
graphos's own server (``_live.graphos_server``) for requests a drain cuts.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import BaseModel

from graphos import changes, jobs, schema_rename, writes
from graphos import drain as drain_module
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.config import Settings
from graphos.contracts.jobs import TERMINAL_STATUSES, Job
from graphos.db import ensure_internal_schema
from graphos.observability.metrics import DRAINING

from ._live import graphos_server
from .conftest import requires_db


class Runs(BaseModel):
    runs: int


# How many times a job of each test tenant has started.
_RUNS: dict[str, int] = {}


async def _long_then_quick(pool: asyncpg.Pool, tenant: str | None, _params: dict) -> BaseModel:
    """Runs for 30 s the first time it starts for a tenant, answers at once after."""
    assert tenant is not None
    _RUNS[tenant] = _RUNS.get(tenant, 0) + 1
    if _RUNS[tenant] == 1:
        async with pool.acquire() as conn:
            await conn.execute("SELECT pg_sleep(30)")
    return Runs(runs=_RUNS[tenant])


async def _sleep(pool: asyncpg.Pool, _tenant: str | None, _params: dict) -> BaseModel:
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_sleep(30)")
    return Runs(runs=0)


KINDS = {
    "drain_long_then_quick": jobs.JobKind(run=_long_then_quick, resumable=True),
    "drain_sleep": jobs.JobKind(run=_sleep, resumable=True),
    "drain_sleep_once_only": jobs.JobKind(run=_sleep),
}


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    if not os.environ.get("GRAPHOS_TEST_DSN"):
        pytest.skip("no GRAPHOS_TEST_DSN")
    p = await asyncpg.create_pool(
        os.environ["GRAPHOS_TEST_DSN"], min_size=1, max_size=12,
        setup=lambda c: c.execute('SET search_path = ag_catalog, "$user", public;'),
    )
    await ensure_internal_schema(p)
    try:
        yield p
    finally:
        await p.execute("DELETE FROM graphos_meta.jobs WHERE tenant LIKE 'draintest%';")
        await p.close()


@pytest.fixture(autouse=True)
def _keep_the_process_drain() -> Iterator[None]:
    """A drained app's drain must not stay the process's for later tests."""
    before = drain_module.current()
    yield
    drain_module.install(before)
    DRAINING.set(0)


def _tenant() -> str:
    return f"draintest_{uuid4().hex[:8]}"


def _runner(pool: asyncpg.Pool, **overrides: float) -> jobs.JobRunner:
    opts = {"concurrency": 1, "poll_s": 0.1, "heartbeat_s": 0.2, "retention_h": 168.0}
    opts.update(overrides)
    return jobs.JobRunner(pool, KINDS, **opts)  # type: ignore[arg-type]


async def _submit(pool: asyncpg.Pool, kind: str, tenant: str) -> Job:
    return await jobs.submit(pool, kind=kind, tenant=tenant, params={}, consumer="test")


async def _wait(pool: asyncpg.Pool, job_id: UUID, statuses: set[str] | frozenset[str]) -> Job:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = await jobs.get(pool, job_id)
        assert job is not None
        if job.status in statuses:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {statuses}")


# ── Jobs ─────────────────────────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_a_drain_hands_a_running_job_back_and_another_runner_finishes_it(
    pool: asyncpg.Pool,
) -> None:
    tenant, later_tenant = _tenant(), _tenant()
    _RUNS[later_tenant] = 1                  # its job answers at once
    first = _runner(pool)
    first.start()
    job = await _submit(pool, "drain_long_then_quick", tenant)
    await _wait(pool, job.id, {"running"})

    await first.begin_drain()
    assert await pool.fetchval(
        "SELECT draining_since IS NOT NULL FROM graphos_meta.jobs WHERE id = $1;", job.id,
    )
    # A draining runner claims nothing more, even with a free slot later.
    later = await _submit(pool, "drain_long_then_quick", later_tenant)

    listener = await asyncpg.connect(os.environ["GRAPHOS_TEST_DSN"])
    notified: asyncio.Queue[str] = asyncio.Queue()
    await listener.add_listener(jobs.JOBS_CHANNEL, lambda *args: notified.put_nowait(args[3]))
    try:
        outcomes = await first.hand_back_all()
        assert outcomes == {"queued": 1}
        assert await asyncio.wait_for(notified.get(), timeout=5) == "drain_long_then_quick"
    finally:
        await listener.close()
    await asyncio.sleep(0.3)
    assert (await jobs.get(pool, later.id)).status == "queued"   # type: ignore[union-attr]

    back = await jobs.get(pool, job.id)
    assert back is not None
    assert back.status == "queued" and back.attempts == 1
    assert back.started_at is None and back.error is None
    assert await pool.fetchval(
        "SELECT draining_since FROM graphos_meta.jobs WHERE id = $1;", job.id,
    ) is None

    second = _runner(pool)
    second.start()
    try:
        done = await _wait(pool, job.id, TERMINAL_STATUSES)
        later_done = await _wait(pool, later.id, TERMINAL_STATUSES)
    finally:
        await second.stop()
    assert done.status == "succeeded" and done.attempts == 1
    assert done.result == {"runs": 2}
    assert later_done.status == "succeeded" and later_done.attempts == 0


@requires_db
@pytest.mark.asyncio
async def test_the_third_interruption_fails_the_job(pool: asyncpg.Pool) -> None:
    # Put back twice already: queued with attempts 2 when this runner claims it.
    job = await _submit(pool, "drain_sleep", _tenant())
    await pool.execute("UPDATE graphos_meta.jobs SET attempts = 2 WHERE id = $1;", job.id)
    runner = _runner(pool)
    runner.start()
    await _wait(pool, job.id, {"running"})
    assert await runner.hand_back_all() == {"failed": 1}
    failed = await jobs.get(pool, job.id)
    assert failed is not None and failed.status == "failed" and failed.attempts == 2
    assert failed.error is not None and failed.error.status == 503
    assert "stopped 3 times" in failed.error.detail


@requires_db
@pytest.mark.asyncio
async def test_a_job_that_may_not_run_twice_fails_when_handed_back(pool: asyncpg.Pool) -> None:
    runner = _runner(pool)
    runner.start()
    job = await _submit(pool, "drain_sleep_once_only", _tenant())
    await _wait(pool, job.id, {"running"})
    assert await runner.hand_back_all() == {"failed": 1}
    failed = await jobs.get(pool, job.id)
    assert failed is not None and failed.status == "failed" and failed.attempts == 0
    assert failed.error is not None
    assert failed.error.detail == "interrupted: the server was shutting down"


@requires_db
@pytest.mark.asyncio
async def test_a_job_asked_to_cancel_is_cancelled_not_queued(pool: asyncpg.Pool) -> None:
    runner = _runner(pool, heartbeat_s=30)   # its heartbeat must not see the request first
    runner.start()
    job = await _submit(pool, "drain_sleep", _tenant())
    await _wait(pool, job.id, {"running"})
    await pool.execute(
        "UPDATE graphos_meta.jobs SET cancel_requested = true WHERE id = $1;", job.id,
    )
    assert await runner.hand_back_all() == {"cancelled": 1}
    assert (await jobs.get(pool, job.id)).status == "cancelled"   # type: ignore[union-attr]


@requires_db
@pytest.mark.asyncio
async def test_the_sweep_puts_back_a_drained_job_whose_process_was_killed(
    pool: asyncpg.Pool,
) -> None:
    drained_tenant = _tenant()
    _RUNS[drained_tenant] = 1                 # answers at once when run again

    async def running_row(tenant: str, *, drained: bool, attempts: int = 0) -> UUID:
        job_id: UUID = await pool.fetchval(
            "INSERT INTO graphos_meta.jobs "
            "(kind, tenant, status, started_at, heartbeat_at, draining_since, attempts) "
            "VALUES ('drain_long_then_quick', $1, 'running', now() - interval '1 hour', "
            "        now() - interval '1 hour', "
            "        CASE WHEN $2 THEN now() - interval '1 hour' END, $3) RETURNING id;",
            tenant, drained, attempts,
        )
        return job_id

    drained = await running_row(drained_tenant, drained=True)
    crashed = await running_row(_tenant(), drained=False)
    exhausted = await running_row(_tenant(), drained=True, attempts=2)
    runner = _runner(pool)
    runner.start()
    try:
        done = await _wait(pool, drained, TERMINAL_STATUSES)
        failed = await _wait(pool, crashed, TERMINAL_STATUSES)
        failed_too = await _wait(pool, exhausted, TERMINAL_STATUSES)
    finally:
        await runner.stop()
    assert done.status == "succeeded" and done.attempts == 1
    assert failed.status == "failed" and failed.attempts == 0
    assert failed_too.status == "failed" and failed_too.attempts == 2


# ── Requests cut by a drain ─────────────────────────────────────────────────


def _app(pool: asyncpg.Pool, **settings: Any) -> FastAPI:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True, **settings))
    app.state.pool = pool
    return app


async def _until(check: Any, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never met")


@requires_db
@pytest.mark.asyncio
async def test_a_synchronous_call_cut_by_a_drain_answers_503_and_is_recorded(
    pool: asyncpg.Pool,
) -> None:
    tenant = _tenant()
    app = _app(pool, shutdown_grace_s=3)   # the cut at 2.25 s

    async def tracked() -> dict[str, int]:
        async def run() -> Runs:
            async with pool.acquire() as conn:
                await conn.execute("SELECT pg_sleep(30)")
            return Runs(runs=0)

        await jobs.run_tracked(
            pool, kind="drain_sleep", tenant=tenant, params={}, consumer="test",
            heartbeat_s=5, run=run,
        )
        return {}

    app.add_api_route("/test/tracked", tracked)
    async with graphos_server(app) as (url, server, task), \
            httpx.AsyncClient(base_url=url, timeout=60) as client:
        call = asyncio.create_task(client.get("/test/tracked"))
        await _until(lambda: pool.fetchval(
            "SELECT count(*) = 1 FROM graphos_meta.jobs WHERE tenant = $1 AND sync "
            "AND status = 'running';", tenant,
        ))
        server.handle_exit(signal.SIGTERM, None)
        answer = await call
        await asyncio.wait_for(task, timeout=10)
    assert answer.status_code == 503 and answer.json()["code"] == "draining"
    row = await pool.fetchrow(
        "SELECT status, error FROM graphos_meta.jobs WHERE tenant = $1 AND sync;", tenant,
    )
    assert row["status"] == "failed"
    assert json.loads(row["error"]) == {
        "status": 503, "detail": "interrupted: the server was shutting down",
    }


@requires_db
@pytest.mark.asyncio
async def test_a_write_cut_in_its_transaction_leaves_nothing(pool: asyncpg.Pool) -> None:
    table = f"draintest_{uuid4().hex[:8]}"
    await pool.execute(f"CREATE TABLE public.{table} (what text);")
    try:
        app = _app(pool, shutdown_grace_s=3)

        async def write() -> dict[str, int]:
            # A row and its record in one transaction, as every graph write does.
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute(f"INSERT INTO public.{table} VALUES ('row');")
                await conn.execute(f"INSERT INTO public.{table} VALUES ('record');")
                await conn.execute("SELECT pg_sleep(30)")
            return {}

        app.add_api_route("/test/write", write, methods=["POST"])
        async with graphos_server(app) as (url, server, task), \
                httpx.AsyncClient(base_url=url, timeout=60) as client:
            call = asyncio.create_task(client.post("/test/write"))
            await _until(lambda: pool.fetchval(
                "SELECT count(*) = 1 FROM pg_stat_activity "
                "WHERE datname = current_database() AND state = 'active' "
                "  AND query = 'SELECT pg_sleep(30)';",
            ))
            server.handle_exit(signal.SIGTERM, None)
            answer = await call
            await asyncio.wait_for(task, timeout=10)
        assert answer.status_code == 503
        assert await pool.fetchval(f"SELECT count(*) FROM public.{table};") == 0
        # Nothing left running or holding a transaction open.
        assert await pool.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "  AND (state LIKE 'idle in transaction%' "
            "       OR (state = 'active' AND query = 'SELECT pg_sleep(30)'));",
        ) == 0
    finally:
        await pool.execute(f"DROP TABLE public.{table};")


@requires_db
@pytest.mark.asyncio
async def test_a_bulk_upload_cut_mid_body_keeps_its_committed_flushes(
    pool: asyncpg.Pool,
) -> None:
    tenant = _tenant()
    await register_tenant(pool, TenantSpec(name=tenant, vertex_labels=("Doc",), edge_labels=()))
    app = _app(pool, shutdown_grace_s=3)

    async def body() -> AsyncIterator[bytes]:
        for i in range(1200):
            yield (json.dumps({"id": f"d{i}", "label": "Doc"}) + "\n").encode()
        # Still sending, as a long upload is, but nothing more to write: blank
        # lines, which the server skips.
        for _ in range(1200):
            await asyncio.sleep(0.05)
            yield b"\n"

    async def upload(client: httpx.AsyncClient) -> int | str:
        try:
            response = await client.post(
                f"/v1/{tenant}/graph/nodes:bulk", content=body(),
                headers={"content-type": "application/x-ndjson"},
            )
        except httpx.TransportError as exc:
            return type(exc).__name__
        return response.status_code

    async with graphos_server(app) as (url, server, task), \
            httpx.AsyncClient(base_url=url, timeout=60) as client:
        sent = asyncio.create_task(upload(client))
        await _until(lambda: pool.fetchval(f'SELECT count(*) = 1000 FROM "{tenant}"."Doc";'))
        server.handle_exit(signal.SIGTERM, None)
        await asyncio.wait_for(task, timeout=10)
        outcome = await asyncio.wait_for(sent, timeout=10)
    # The client was still sending: it sees the connection end, or the 503.
    assert outcome in (503, "ReadError", "WriteError", "RemoteProtocolError")
    assert await pool.fetchval(f'SELECT count(*) FROM "{tenant}"."Doc";') == 1000
    async with pool.acquire() as conn:
        page = await changes.read(conn, tenant, None, 1000)
    assert sum(c.count for c in page.changes if c.kind == "node") == 1000


@requires_db
@pytest.mark.asyncio
async def test_a_property_rename_handed_back_stays_running_and_finishes_when_run_again(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = _tenant()
    await register_tenant(pool, TenantSpec(name=tenant, vertex_labels=("Doc",), edge_labels=()))
    await writes.upsert_nodes(pool, tenant=tenant, label="Doc", rows=[
        {"id": f"d{i}", "props": {"title": f"t{i}"}} for i in range(5)
    ])
    # A kind name of its own: a graphos-api running on the same database (the
    # test stack's) knows ``rename_property`` and would claim the job put back.
    kinds = {"drain_rename_property": jobs.JobKind(
        run=schema_rename.run_property_job, resumable=True,
    )}
    batch = schema_rename.writes.rename_property_batch
    hold = asyncio.Event()

    async def held_batch(*args: Any, **kwargs: Any) -> int:
        if not hold.is_set():
            await asyncio.sleep(30)          # the first run is cut here
        return await batch(*args, **kwargs)

    monkeypatch.setattr(schema_rename.writes, "rename_property_batch", held_batch)
    job_id = UUID(await schema_rename.start_property(
        pool, tenant, "Doc", "title", "headline", by="test",
    ))
    await pool.execute(
        "UPDATE graphos_meta.jobs SET kind = 'drain_rename_property' WHERE id = $1;", job_id,
    )
    first = jobs.JobRunner(pool, kinds, concurrency=1, poll_s=0.1, heartbeat_s=0.2,
                           retention_h=168.0)
    first.start()
    await _wait(pool, job_id, {"running"})
    await asyncio.sleep(0.3)                 # inside its first batch
    assert await first.hand_back_all() == {"queued": 1}
    # The rename is not marked failed: it goes on when the job runs again.
    stored = json.loads(await pool.fetchval(
        "SELECT spec::text FROM graphos_meta.tenants WHERE name = $1;", tenant,
    ))
    assert stored["renames"][-1]["status"] == "running"

    hold.set()
    second = jobs.JobRunner(pool, kinds, concurrency=1, poll_s=0.1, heartbeat_s=0.2,
                            retention_h=168.0)
    second.start()
    try:
        done = await _wait(pool, job_id, TERMINAL_STATUSES)
    finally:
        await second.stop()
    assert done.status == "succeeded", done.error
    assert done.attempts == 1
    assert done.result is not None and done.result["rows"] == 5
    stored = json.loads(await pool.fetchval(
        "SELECT spec::text FROM graphos_meta.tenants WHERE name = $1;", tenant,
    ))
    assert stored["renames"][-1]["status"] == "done"
    titles = await pool.fetchval(
        f'SELECT count(*) FROM "{tenant}"."Doc" WHERE (properties::text)::jsonb ? \'headline\';',
    )
    assert titles == 5

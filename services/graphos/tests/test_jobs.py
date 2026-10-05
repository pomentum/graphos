"""Background jobs: store, runner and the ``Prefer: respond-async`` answer.

The runner tests are DB-gated and use test-only job kinds, so they cover
the job machinery without depending on Louvain or the resolver.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import BaseModel
from starlette.requests import Request

from graphos import jobs
from graphos.api.db import vacuum
from graphos.api.jobs import wants_async
from graphos.auth import TokenPrincipal
from graphos.contracts.db import VacuumRequest
from graphos.contracts.jobs import TERMINAL_STATUSES, Job
from graphos.db import ensure_internal_schema

from .conftest import requires_db

ADMIN = TokenPrincipal(consumer="test", tenants=frozenset(), admin=True)


class Echo(BaseModel):
    tenant: str | None
    params: dict


async def _echo(_pool: asyncpg.Pool, tenant: str | None, params: dict) -> BaseModel:
    return Echo(tenant=tenant, params=params)


async def _busy(_pool: asyncpg.Pool, _tenant: str | None, _params: dict) -> BaseModel:
    raise HTTPException(status_code=409, detail="busy")


async def _crash(_pool: asyncpg.Pool, _tenant: str | None, _params: dict) -> BaseModel:
    raise RuntimeError("secret database text")


async def _sleep(pool: asyncpg.Pool, _tenant: str | None, _params: dict) -> BaseModel:
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_sleep(30)")
    return Echo(tenant=None, params={})


KINDS = {
    "echo": jobs.JobKind(run=_echo),
    "busy": jobs.JobKind(run=_busy),
    "crash": jobs.JobKind(run=_crash),
    "sleep": jobs.JobKind(run=_sleep),
}


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    if not os.environ.get("GRAPHOS_TEST_DSN"):
        pytest.skip("no GRAPHOS_TEST_DSN")
    p = await asyncpg.create_pool(dsn=os.environ["GRAPHOS_TEST_DSN"], min_size=1, max_size=6)
    await ensure_internal_schema(p)
    await p.execute("DELETE FROM graphos_meta.jobs WHERE tenant LIKE 'jobtest%' OR tenant IS NULL;")
    try:
        yield p
    finally:
        await p.execute(
            "DELETE FROM graphos_meta.jobs WHERE tenant LIKE 'jobtest%' OR tenant IS NULL;"
        )
        await p.close()


def _runner(pool: asyncpg.Pool, **overrides: float) -> jobs.JobRunner:
    opts = {"concurrency": 2, "poll_s": 0.1, "heartbeat_s": 0.2, "retention_h": 168.0}
    opts.update(overrides)
    return jobs.JobRunner(pool, KINDS, **opts)  # type: ignore[arg-type]


def _tenant() -> str:
    return f"jobtest_{uuid4().hex[:8]}"


async def _wait(pool: asyncpg.Pool, job_id: UUID, statuses: set[str], timeout: float = 10) -> Job:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = await jobs.get(pool, job_id)
        assert job is not None
        if job.status in statuses:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {statuses}")


async def _submit(pool: asyncpg.Pool, kind: str, tenant: str | None = None) -> Job:
    return await jobs.submit(
        pool, kind=kind, tenant=tenant or _tenant(), params={"x": 1}, consumer="test",
    )


def test_wants_async_reads_the_prefer_header() -> None:
    def req(*values: str) -> Request:
        headers = [(b"prefer", v.encode()) for v in values]
        return Request({"type": "http", "headers": headers})

    assert wants_async(req("respond-async"))
    assert wants_async(req("wait=10, Respond-Async"))
    assert wants_async(req("return=minimal", "respond-async; foo=bar"))
    assert not wants_async(req())
    assert not wants_async(req("return=minimal"))


@requires_db
@pytest.mark.asyncio
async def test_job_runs_and_records_the_result(pool: asyncpg.Pool) -> None:
    runner = _runner(pool)
    runner.start()
    try:
        tenant = _tenant()
        job = await _submit(pool, "echo", tenant)
        assert job.status == "queued"
        runner.notify()
        done = await _wait(pool, job.id, TERMINAL_STATUSES)
    finally:
        await runner.stop()
    assert done.status == "succeeded"
    assert done.result == {"tenant": tenant, "params": {"x": 1}}
    assert done.error is None and done.started_at and done.finished_at


@requires_db
@pytest.mark.asyncio
async def test_failures_record_status_and_detail(pool: asyncpg.Pool) -> None:
    runner = _runner(pool)
    runner.start()
    try:
        busy = await _submit(pool, "busy")
        crash = await _submit(pool, "crash")
        busy = await _wait(pool, busy.id, TERMINAL_STATUSES)
        crash = await _wait(pool, crash.id, TERMINAL_STATUSES)
    finally:
        await runner.stop()
    assert (busy.status, busy.error.status, busy.error.detail) == ("failed", 409, "busy")
    assert (crash.status, crash.error.status) == ("failed", 500)
    assert "secret" not in crash.error.detail


@requires_db
@pytest.mark.asyncio
async def test_cancelling_a_queued_job_is_immediate_and_it_never_runs(pool: asyncpg.Pool) -> None:
    job = await _submit(pool, "echo")
    cancelled = await jobs.request_cancel(pool, job.id)
    assert cancelled is not None and cancelled.status == "cancelled"
    runner = _runner(pool)
    runner.start()
    try:
        await asyncio.sleep(0.5)
    finally:
        await runner.stop()
    after = await jobs.get(pool, job.id)
    assert after.status == "cancelled" and after.started_at is None
    assert await jobs.request_cancel(pool, job.id) is None   # already finished


async def _sleeping_queries(pool: asyncpg.Pool) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE query = 'SELECT pg_sleep(30)' AND state = 'active';"
    )


@requires_db
@pytest.mark.asyncio
async def test_cancelling_a_running_job_stops_its_query(pool: asyncpg.Pool) -> None:
    runner = _runner(pool)
    runner.start()
    try:
        job = await _submit(pool, "sleep")
        await _wait(pool, job.id, {"running"})
        await asyncio.sleep(0.3)
        assert await _sleeping_queries(pool) == 1
        started = time.monotonic()
        await jobs.request_cancel(pool, job.id)
        runner.cancel_local(job.id)
        done = await _wait(pool, job.id, TERMINAL_STATUSES)
        assert time.monotonic() - started < 5
    finally:
        await runner.stop()
    assert done.status == "cancelled"
    await asyncio.sleep(0.3)
    assert await _sleeping_queries(pool) == 0


@requires_db
@pytest.mark.asyncio
async def test_another_replica_cancels_through_the_heartbeat(pool: asyncpg.Pool) -> None:
    runner = _runner(pool)
    runner.start()
    try:
        job = await _submit(pool, "sleep")
        await _wait(pool, job.id, {"running"})
        await jobs.request_cancel(pool, job.id)   # flag only, no cancel_local
        done = await _wait(pool, job.id, TERMINAL_STATUSES, timeout=5)
    finally:
        await runner.stop()
    assert done.status == "cancelled"


@requires_db
@pytest.mark.asyncio
async def test_stopping_the_server_fails_running_jobs(pool: asyncpg.Pool) -> None:
    runner = _runner(pool)
    runner.start()
    job = await _submit(pool, "sleep")
    await _wait(pool, job.id, {"running"})
    await runner.stop()
    done = await jobs.get(pool, job.id)
    assert done.status == "failed" and done.error.status == 503


@requires_db
@pytest.mark.asyncio
async def test_one_recompute_queued_or_running_per_tenant(pool: asyncpg.Pool) -> None:
    tenant = _tenant()
    first = await _submit(pool, "recompute_communities", tenant)
    with pytest.raises(jobs.JobConflictError) as caught:
        await _submit(pool, "recompute_communities", tenant)
    assert caught.value.existing == first.id
    await _submit(pool, "recompute_communities")   # another tenant is fine
    await _submit(pool, "echo", tenant)            # another kind is fine
    await jobs.request_cancel(pool, first.id)
    await _submit(pool, "recompute_communities", tenant)   # finished: allowed again


@requires_db
@pytest.mark.asyncio
async def test_sweep_fails_silent_jobs_and_deletes_old_ones(pool: asyncpg.Pool) -> None:
    silent, old = (
        await pool.fetchval(
            "INSERT INTO graphos_meta.jobs (kind, tenant, status, started_at, heartbeat_at) "
            "VALUES ('echo', $1, 'running', now() - interval '1 hour', "
            "        now() - interval '1 hour') RETURNING id;",
            _tenant(),
        ),
        await pool.fetchval(
            "INSERT INTO graphos_meta.jobs (kind, tenant, status, finished_at) "
            "VALUES ('echo', $1, 'succeeded', now() - interval '10 days') RETURNING id;",
            _tenant(),
        ),
    )
    runner = _runner(pool, retention_h=24.0)
    runner.start()
    try:
        failed = await _wait(pool, silent, TERMINAL_STATUSES)
        # The same sweep deletes old jobs right after marking silent ones.
        deadline = time.monotonic() + 5
        while await jobs.get(pool, old) is not None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
    finally:
        await runner.stop()
    assert failed.status == "failed" and "interrupted" in failed.error.detail
    assert await jobs.get(pool, old) is None


@requires_db
@pytest.mark.asyncio
async def test_respond_async_queues_and_answers_202(pool: asyncpg.Pool) -> None:
    notified: list[bool] = []
    runner = SimpleNamespace(notify=lambda: notified.append(True))
    state = SimpleNamespace(pool=pool, job_runner=runner)
    request = Request({
        "type": "http", "headers": [(b"prefer", b"respond-async")],
        "app": SimpleNamespace(state=state),
    })
    response = await vacuum(VacuumRequest(analyze=True), request, ADMIN)
    assert response.status_code == 202
    assert response.headers["preference-applied"] == "respond-async"
    job_id = UUID(response.headers["location"].rsplit("/", 1)[-1])
    job = await jobs.get(pool, job_id)
    assert (job.kind, job.tenant, job.status, job.consumer) == ("vacuum", None, "queued", "test")
    assert job.params == {"tables": None, "full": False, "analyze": True}
    assert notified == [True]


def test_job_visibility() -> None:
    from datetime import UTC, datetime

    from graphos.api.jobs import _visible

    def job(tenant: str | None) -> Job:
        return Job(id=uuid4(), kind="echo", tenant=tenant, status="queued",
                   created_at=datetime.now(UTC))

    reader = TokenPrincipal(consumer="r", tenants=frozenset({"a"}), admin=False)
    assert _visible(reader, job("a"))
    assert not _visible(reader, job("b"))
    assert not _visible(reader, job(None))          # instance-wide: admins only
    assert _visible(ADMIN, job("b")) and _visible(ADMIN, job(None))


@requires_db
@pytest.mark.asyncio
async def test_a_runner_leaves_kinds_it_does_not_know(pool: asyncpg.Pool) -> None:
    unknown = await _submit(pool, "a_newer_kind")
    known = await _submit(pool, "echo")
    runner = _runner(pool)
    runner.start()
    try:
        await _wait(pool, known.id, TERMINAL_STATUSES)
        await asyncio.sleep(0.3)
    finally:
        await runner.stop()
    assert (await jobs.get(pool, unknown.id)).status == "queued"


@requires_db
@pytest.mark.asyncio
async def test_cancel_needs_admin_for_admin_and_unknown_kinds(pool: asyncpg.Pool) -> None:
    from graphos.api.jobs import cancel_job

    tenant = _tenant()
    member = TokenPrincipal(consumer="member", tenants=frozenset({tenant}), admin=False)
    runner = SimpleNamespace(
        kinds={"echo": jobs.JobKind(run=_echo), "recompute_communities": jobs.JobKind(
            run=_echo, admin_only=True)},
        cancel_local=lambda _job_id: None,
    )
    request = Request({"type": "http", "headers": [],
                       "app": SimpleNamespace(state=SimpleNamespace(pool=pool, job_runner=runner))})
    for kind in ("recompute_communities", "a_newer_kind"):
        job = await _submit(pool, kind, tenant)
        with pytest.raises(HTTPException) as denied:
            await cancel_job(job.id, request, member)
        assert denied.value.status_code == 403
        assert (await cancel_job(job.id, request, ADMIN)).status == "cancelled"
    job = await _submit(pool, "echo", tenant)
    assert (await cancel_job(job.id, request, member)).status == "cancelled"

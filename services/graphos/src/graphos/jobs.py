"""Background jobs: long operations a caller asked to run asynchronously.

A request carrying ``Prefer: respond-async`` on a long endpoint (community
recompute, rule-based edge resolution, vacuum) is validated as usual, then
stored as a job in ``graphos_meta.jobs`` and answered ``202 Accepted`` with
``Location: /v1/jobs/{id}``.  A :class:`JobRunner` in every API process
claims queued jobs (``FOR UPDATE SKIP LOCKED``, so replicas never run the
same job twice), runs them with the same code the synchronous endpoint
uses, and records the outcome: on success the body the synchronous call
would have returned, on failure its HTTP status and detail.

Lifecycle: ``queued`` -> ``running`` -> ``succeeded`` | ``failed`` |
``cancelled``.  Cancelling a queued job is immediate; a running job's
task is cancelled, which cancels its database statement and terminates a
community computation's process (the process running the job learns of
the request at once if it is the same process, otherwise within one
heartbeat).  A running job whose heartbeat stops (its process died) is
marked failed by any replica; jobs are not retried automatically.
Finished jobs are deleted after the retention period.

The same kinds called synchronously are recorded too (:func:`run_tracked`,
``sync``): the caller still waits for the response, but the call is listed
as running while it runs, can be cancelled like a job, and keeps its
outcome, so everyone with access to the tenant sees what is running.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar, cast
from uuid import UUID

import asyncpg
from fastapi import HTTPException
from pydantic import BaseModel

from graphos.changes import ACTOR, Actor
from graphos.contracts.jobs import Job

log = logging.getLogger(__name__)

JobFn = Callable[[asyncpg.Pool, str | None, dict[str, Any]], Awaitable[BaseModel]]
ResponseT = TypeVar("ResponseT", bound=BaseModel)


@dataclass(frozen=True)
class JobKind:
    """How to run one kind of job.

    ``run(pool, tenant, params)`` returns the response model of the
    synchronous endpoint, or raises ``HTTPException`` as that endpoint
    would.  ``needs`` is the access starting it needs, and so cancelling
    it: ``"write"`` or ``"maintain"`` on its tenant, or ``"admin"``.
    """

    run: JobFn
    needs: str = "write"


# Kinds that exclude each other per tenant (the unique index
# jobs_one_active_graph_job in graphos.db): both rewrite every node.
_GRAPH_JOBS = ("recompute_communities", "compute_metrics")
EXCLUSIVE: dict[str, tuple[str, ...]] = {kind: _GRAPH_JOBS for kind in _GRAPH_JOBS}


class JobConflictError(Exception):
    """An equivalent job is already queued or running."""

    def __init__(self, existing: UUID) -> None:
        super().__init__(f"job {existing} is already queued or running")
        self.existing = existing


_COLUMNS = (
    "id, kind, tenant, status, params::text AS params, result::text AS result, "
    "error::text AS error, consumer, cancel_requested, sync, created_at, started_at, "
    "finished_at"
)


def _to_job(row: asyncpg.Record) -> Job:
    data = dict(row)
    for key in ("params", "result", "error"):
        data[key] = json.loads(data[key]) if data[key] is not None else None
    data["params"] = data["params"] or {}
    return Job.model_validate(data)


# ── Store ────────────────────────────────────────────────────────────────────


async def submit(
    pool: asyncpg.Pool,
    *,
    kind: str,
    tenant: str | None,
    params: Mapping[str, Any],
    consumer: str | None,
) -> Job:
    """Queue a job.  Raises :class:`JobConflictError` if a one-at-a-time
    kind already has a queued or running job for this tenant."""
    try:
        row = await pool.fetchrow(
            f"INSERT INTO graphos_meta.jobs (kind, tenant, params, consumer) "
            f"VALUES ($1, $2, $3::jsonb, $4) RETURNING {_COLUMNS};",
            kind, tenant, json.dumps(dict(params)), consumer,
        )
    except asyncpg.exceptions.UniqueViolationError:
        raise JobConflictError(await _active_of(pool, kind, tenant)) from None
    return _to_job(row)


async def _active_of(pool: asyncpg.Pool, kind: str, tenant: str | None) -> UUID:
    """The queued or running job that ``kind`` cannot run beside."""
    job_id: UUID = await pool.fetchval(
        "SELECT id FROM graphos_meta.jobs "
        "WHERE kind = ANY($1::text[]) AND tenant IS NOT DISTINCT FROM $2 "
        "  AND status IN ('queued', 'running') "
        "ORDER BY created_at LIMIT 1;",
        list(EXCLUSIVE.get(kind, (kind,))), tenant,
    )
    return job_id


async def get(pool: asyncpg.Pool, job_id: UUID) -> Job | None:
    row = await pool.fetchrow(
        f"SELECT {_COLUMNS} FROM graphos_meta.jobs WHERE id = $1;", job_id,
    )
    return _to_job(row) if row else None


async def list_jobs(
    pool: asyncpg.Pool,
    *,
    tenants: list[str] | None,
    status: Sequence[str] | None,
    limit: int,
) -> list[Job]:
    """Newest first.  ``tenants=None`` lists every job, instance-wide ones
    included; a list restricts to those tenants.  ``status``: any of these."""
    rows = await pool.fetch(
        f"SELECT {_COLUMNS} FROM graphos_meta.jobs "
        f"WHERE ($1::text[] IS NULL OR tenant = ANY($1::text[])) "
        f"  AND ($2::text[] IS NULL OR status = ANY($2::text[])) "
        f"ORDER BY created_at DESC LIMIT $3;",
        tenants, list(status) if status else None, limit,
    )
    return [_to_job(r) for r in rows]


async def request_cancel(pool: asyncpg.Pool, job_id: UUID) -> Job | None:
    """Cancel a queued job now; flag a running one.  Returns the job after
    the change, or ``None`` if it is already finished (or absent)."""
    row = await pool.fetchrow(
        f"UPDATE graphos_meta.jobs SET "
        f"  cancel_requested = true, "
        f"  status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END, "
        f"  finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END "
        f"WHERE id = $1 AND status IN ('queued', 'running') "
        f"RETURNING {_COLUMNS};",
        job_id,
    )
    return _to_job(row) if row else None


# ── Synchronous calls ────────────────────────────────────────────────────────

# Synchronous calls running in this process: job id -> the request's task.
_TRACKED: dict[UUID, asyncio.Task[Any]] = {}
# Those of them cancelled because a cancel was requested.  Both the cancel
# endpoint and the heartbeat notice a request; the task is cancelled once
# only, so a second cancel cannot interrupt the call's 409 answer.
_CANCELLING: set[UUID] = set()


def _cancel_once(job_id: UUID, task: asyncio.Task[Any]) -> None:
    if job_id not in _CANCELLING:
        _CANCELLING.add(job_id)
        task.cancel()


async def run_tracked(  # noqa: PLR0913 — the job's fields and how to run it
    pool: asyncpg.Pool,
    *,
    kind: str,
    tenant: str | None,
    params: Mapping[str, Any],
    consumer: str | None,
    heartbeat_s: float,
    run: Callable[[], Awaitable[ResponseT]],
) -> ResponseT:
    """Run a synchronous call of a job kind, recorded as a job (``sync``).

    The caller waits for ``run()`` as before.  Meanwhile the call is listed
    as running, with a heartbeat (so a process that dies leaves no running
    row), and a cancel request cancels it: the caller then gets ``409``.
    The outcome is recorded like a background job's.  Raises
    :class:`JobConflictError` like :func:`submit`.  Listing is best-effort:
    if the row cannot be written, the call runs unlisted.
    """
    try:
        job_id: UUID = await pool.fetchval(
            "INSERT INTO graphos_meta.jobs "
            "(kind, tenant, params, consumer, status, sync, started_at, heartbeat_at) "
            "VALUES ($1, $2, $3::jsonb, $4, 'running', true, now(), now()) RETURNING id;",
            kind, tenant, json.dumps(dict(params)), consumer,
        )
    except asyncpg.exceptions.UniqueViolationError:
        raise JobConflictError(await _active_of(pool, kind, tenant)) from None
    except Exception:  # noqa: BLE001 — listing the call must never stop it
        log.warning(
            "could not list a synchronous call as a job", extra={"kind": kind}, exc_info=True,
        )
        return await run()
    task = asyncio.current_task()
    if task is not None:
        _TRACKED[job_id] = task
    beat = asyncio.create_task(_beat(pool, job_id, task, heartbeat_s))
    status: str = "failed"
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    try:
        response = await run()
        status, result = "succeeded", response.model_dump(mode="json")
        return response
    except asyncio.CancelledError:
        beat.cancel()                     # it must not cancel the call again
        if job_id in _CANCELLING or await pool.fetchval(
            "SELECT cancel_requested FROM graphos_meta.jobs WHERE id = $1;", job_id,
        ):
            if task is not None:
                task.uncancel()
            status = "cancelled"
            raise HTTPException(
                status_code=409, detail=f"cancelled: job {job_id} was cancelled while running",
            ) from None
        error = {"status": 503, "detail": "interrupted: the request was cancelled"}
        raise
    except HTTPException as exc:
        error = {"status": exc.status_code, "detail": str(exc.detail)}
        raise
    except asyncpg.exceptions.QueryCanceledError:
        error = {"status": 504, "detail": "a database statement exceeded its timeout"}
        raise
    except Exception:
        error = {"status": 500, "detail": "internal error; see the server log"}
        raise
    finally:
        beat.cancel()
        _TRACKED.pop(job_id, None)
        _CANCELLING.discard(job_id)
        try:
            await _finish(pool, job_id, status, result, error)
        except Exception:  # noqa: BLE001 — the call's own outcome comes first
            log.warning("recording a synchronous job failed", extra={"job_id": str(job_id)})


async def _finish(
    pool: asyncpg.Pool,
    job_id: UUID,
    status: str,
    result: dict[str, Any] | None,
    error: dict[str, Any] | None,
) -> None:
    await pool.execute(
        "UPDATE graphos_meta.jobs SET status = $2, finished_at = now(), "
        "  result = $3::jsonb, error = $4::jsonb "
        "WHERE id = $1 AND status = 'running';",
        job_id, status,
        json.dumps(result) if result is not None else None,
        json.dumps(error) if error is not None else None,
    )


async def _beat(
    pool: asyncpg.Pool, job_id: UUID, task: asyncio.Task[Any] | None, heartbeat_s: float,
) -> None:
    """Keep a running job's heartbeat; cancel ``task`` when a cancel is requested."""
    while True:
        await asyncio.sleep(heartbeat_s)
        try:
            cancel = await pool.fetchval(
                "UPDATE graphos_meta.jobs SET heartbeat_at = now() "
                "WHERE id = $1 RETURNING cancel_requested;",
                job_id,
            )
        except Exception:  # noqa: BLE001 — try again next beat
            log.warning("job heartbeat failed", extra={"job_id": str(job_id)})
            continue
        if cancel and task is not None:
            _cancel_once(job_id, task)
            return


def cancel_tracked(job_id: UUID) -> bool:
    """Cancel a synchronous call now if this process runs it."""
    task = _TRACKED.get(job_id)
    if task is None:
        return False
    _cancel_once(job_id, task)
    return True


# ── Runner ───────────────────────────────────────────────────────────────────


class JobRunner:
    """Claims and runs queued jobs in this process, at most ``concurrency``
    at a time."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        kinds: Mapping[str, JobKind],
        *,
        concurrency: int,
        poll_s: float,
        heartbeat_s: float,
        retention_h: float,
    ) -> None:
        self.kinds = dict(kinds)
        self._pool = pool
        self._concurrency = concurrency
        self._poll_s = poll_s
        self._heartbeat_s = heartbeat_s
        self._retention_h = retention_h
        self._wake = asyncio.Event()
        self._active: dict[UUID, asyncio.Task[None]] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._stopping = False

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop(), name="graphos-jobs")

    async def stop(self) -> None:
        """Stop claiming; running jobs are interrupted and recorded as failed."""
        self._stopping = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def notify(self) -> None:
        """A job was queued by this process: look now rather than at the next poll."""
        self._wake.set()

    def cancel_local(self, job_id: UUID) -> None:
        """Cancel the job, or the synchronous call, now if this process runs it."""
        task = self._active.get(job_id)
        if task is not None:
            task.cancel()
        else:
            cancel_tracked(job_id)

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self._sweep()
                while len(self._active) < self._concurrency:
                    row = await self._claim()
                    if row is None:
                        break
                    job_id: UUID = row["id"]
                    task = asyncio.create_task(self._run(row), name=f"graphos-job-{job_id}")
                    self._active[job_id] = task

                    def _forget(_t: asyncio.Task[None], i: UUID = job_id) -> None:
                        self._done(i)

                    task.add_done_callback(_forget)
            except Exception:  # noqa: BLE001 — the loop must survive a bad cycle
                log.exception("job loop cycle failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_s)
            self._wake.clear()

    def _done(self, job_id: UUID) -> None:
        self._active.pop(job_id, None)
        self._wake.set()   # a slot is free

    async def _claim(self) -> asyncpg.Record | None:
        # Only kinds this process can run: during a rolling upgrade an older
        # replica leaves a newer kind for a replica that knows it.
        return await self._pool.fetchrow(
            "UPDATE graphos_meta.jobs SET status = 'running', "
            "  started_at = now(), heartbeat_at = now() "
            "WHERE id = ("
            "  SELECT id FROM graphos_meta.jobs "
            "  WHERE status = 'queued' AND kind = ANY($1::text[]) "
            "  ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
            ") RETURNING id, kind, tenant, consumer, params::text AS params;",
            list(self.kinds),
        )

    async def _sweep(self) -> None:
        stale = await self._pool.execute(
            "UPDATE graphos_meta.jobs SET status = 'failed', finished_at = now(), "
            "  error = jsonb_build_object('status', 500, 'detail', "
            "    'interrupted: the process running the job stopped') "
            "WHERE status = 'running' "
            "  AND heartbeat_at < now() - make_interval(secs => $1);",
            3 * self._heartbeat_s,
        )
        if stale != "UPDATE 0":
            log.warning("jobs marked failed after their heartbeat stopped: %s", stale)
        await self._pool.execute(
            "DELETE FROM graphos_meta.jobs "
            "WHERE finished_at < now() - make_interval(secs => $1);",
            self._retention_h * 3600,
        )

    async def _run(self, row: asyncpg.Record) -> None:
        job_id: UUID = row["id"]
        kind = self.kinds.get(row["kind"])
        # Change records of the job's writes name who submitted it.  This
        # task's own context: set here, it is seen by everything it awaits.
        ACTOR.set(Actor(consumer=row["consumer"], trace_id=f"job:{job_id}"))
        heartbeat = asyncio.create_task(
            _beat(self._pool, job_id, asyncio.current_task(), self._heartbeat_s),
        )
        result: dict[str, Any] | None = None
        error: dict[str, Any] | None = None
        status = "failed"
        try:
            if kind is None:
                error = {"status": 500, "detail": f"unknown job kind {row['kind']!r}"}
            else:
                response = await kind.run(self._pool, row["tenant"], json.loads(row["params"]))
                result, status = response.model_dump(mode="json"), "succeeded"
        except asyncio.CancelledError:
            current = cast("asyncio.Task[Any]", asyncio.current_task())
            current.uncancel()   # so the outcome can still be recorded
            if self._stopping:
                error = {"status": 503, "detail": "interrupted: the server was shutting down"}
            else:
                status = "cancelled"
        except HTTPException as exc:
            error = {"status": exc.status_code, "detail": str(exc.detail)}
        except asyncpg.exceptions.QueryCanceledError:
            error = {"status": 504, "detail": "a database statement exceeded its timeout"}
        except Exception:  # noqa: BLE001 — recorded on the job, not raised
            log.exception("job failed", extra={"job_id": str(job_id), "kind": row["kind"]})
            error = {"status": 500, "detail": "internal error; see the server log"}
        finally:
            heartbeat.cancel()

        await _finish(self._pool, job_id, status, result, error)
        log.info(
            "job finished",
            extra={"job_id": str(job_id), "kind": row["kind"], "status": status},
        )

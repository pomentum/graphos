"""Background job endpoints, and the ``Prefer: respond-async`` answer.

``GET /v1/jobs/{id}``, ``GET /v1/jobs`` and ``POST /v1/jobs/{id}:cancel``.
A job is visible to admins and to tokens with access to its tenant;
instance-wide jobs (vacuum) to admins only.  A job the caller may not see
answers 404, as if it did not exist.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypeVar
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from graphos import jobs
from graphos.auth import ReadPrincipal, TokenPrincipal, WritePrincipal
from graphos.contracts.jobs import Job, JobList, JobStatus

router = APIRouter(tags=["jobs"], prefix="/jobs")

ResponseT = TypeVar("ResponseT", bound=BaseModel)

# Shown on the endpoints that accept ``Prefer: respond-async``.
ACCEPTED_RESPONSE: dict[int | str, dict[str, Any]] = {
    202: {
        "model": Job,
        "description": (
            "Sent `Prefer: respond-async`: the request was validated and queued "
            "as a job. Poll `Location` (`GET /v1/jobs/{id}`); `Retry-After` "
            "suggests an interval in seconds."
        ),
    },
}


def wants_async(request: Request) -> bool:
    """True when the ``Prefer`` header asks for ``respond-async`` (RFC 7240)."""
    for header in request.headers.getlist("prefer"):
        for pref in header.split(","):
            if pref.split(";", 1)[0].split("=", 1)[0].strip().lower() == "respond-async":
                return True
    return False


async def respond_async(
    request: Request,
    *,
    kind: str,
    tenant: str | None,
    body: BaseModel,
    principal: TokenPrincipal,
) -> JSONResponse:
    """Queue ``kind`` with ``body`` as its params and answer 202."""
    try:
        job = await jobs.submit(
            request.app.state.pool, kind=kind, tenant=tenant,
            params=body.model_dump(mode="json"), consumer=principal.consumer,
        )
    except jobs.JobConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a {kind} job is already queued or running: {exc.existing}",
        ) from exc
    request.app.state.job_runner.notify()
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=job.model_dump(mode="json"),
        headers={
            "Location": f"/v1/jobs/{job.id}",
            "Retry-After": "2",
            "Preference-Applied": "respond-async",
        },
    )


async def run_sync(  # noqa: PLR0913 — the job's fields and how to run it
    request: Request,
    *,
    kind: str,
    tenant: str | None,
    body: BaseModel,
    principal: TokenPrincipal,
    run: Callable[[], Awaitable[ResponseT]],
) -> ResponseT:
    """Run a synchronous call of ``kind``, listed in ``/v1/jobs`` while it runs."""
    try:
        return await jobs.run_tracked(
            request.app.state.pool, kind=kind, tenant=tenant,
            params=body.model_dump(mode="json"), consumer=principal.consumer,
            heartbeat_s=request.app.state.settings.job_heartbeat_s, run=run,
        )
    except jobs.JobConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a {kind} job is already queued or running: {exc.existing}",
        ) from exc


def _visible(principal: TokenPrincipal, job: Job) -> bool:
    if principal.admin:
        return True
    return job.tenant is not None and job.tenant in principal.tenants


def _not_found(job_id: UUID) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no job {job_id}")


@router.get("/{job_id}", response_model=Job, summary="One job's status and outcome")
async def get_job(job_id: UUID, request: Request, principal: ReadPrincipal) -> Job:
    job = await jobs.get(request.app.state.pool, job_id)
    if job is None or not _visible(principal, job):
        raise _not_found(job_id)
    return job


@router.get("", response_model=JobList, summary="Recent jobs, newest first")
async def list_jobs(
    request: Request,
    principal: ReadPrincipal,
    tenant: Annotated[str | None, Query(description="Only this tenant's jobs.")] = None,
    status_: Annotated[
        list[JobStatus] | None,
        Query(alias="status", description="Only jobs in one of these states (repeatable)."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> JobList:
    if tenant is not None:
        if not principal.admin and tenant not in principal.tenants:
            return JobList()
        tenants: list[str] | None = [tenant]
    else:
        tenants = None if principal.admin else sorted(principal.tenants)
    found = await jobs.list_jobs(
        request.app.state.pool, tenants=tenants, status=status_, limit=limit,
    )
    return JobList(jobs=found)


@router.post(
    "/{job_id}:cancel",
    response_model=Job,
    summary="Cancel a queued or running job",
    description=(
        "A queued job is cancelled at once. A running job is stopped: its "
        "database statement is cancelled and a community computation's process "
        "is terminated; poll the job for its final status. 409 if the job has "
        "already finished. Cancelling a kind that needs admin scope to start "
        "needs admin scope."
    ),
)
async def cancel_job(job_id: UUID, request: Request, principal: WritePrincipal) -> Job:
    pool = request.app.state.pool
    runner: jobs.JobRunner = request.app.state.job_runner
    job = await jobs.get(pool, job_id)
    if job is None or not _visible(principal, job):
        raise _not_found(job_id)
    # A kind this replica does not know (mid rolling upgrade) counts as
    # admin-only: fail closed.
    kind = runner.kinds.get(job.kind)
    if (kind is None or kind.admin_only) and not principal.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin scope required")
    updated = await jobs.request_cancel(pool, job_id)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"job {job_id} has already finished ({job.status})",
        )
    runner.cancel_local(job_id)
    return updated

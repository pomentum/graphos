"""Pydantic contracts for background jobs (``Prefer: respond-async``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})


class JobError(BaseModel):
    """Why a job failed: the HTTP status and detail the synchronous call
    would have answered with."""

    status: int = Field(..., description="The HTTP status code, such as 409 or 500.")
    detail: str = Field(..., description="The error's detail message.")


class JobProgress(BaseModel):
    """How much of a long job is done (a property rename reports it)."""

    done: int = Field(..., description="Items done so far (rows rewritten).")
    total: int | None = Field(None, description="Items to do, when known.")


class Job(BaseModel):
    """A long operation run in the background, or a synchronous call being tracked."""

    id: UUID = Field(..., description="The job's id, as in `Location: /v1/jobs/{id}`.")
    kind: str = Field(
        ..., description=(
            "`recompute_communities`, `compute_metrics`, `resolve_edges`, `check_properties`, "
            "`rename_property` or `vacuum`."
        ),
    )
    tenant: str | None = Field(
        default=None, description="Null for instance-wide jobs (vacuum).",
    )
    status: JobStatus = Field(
        ...,
        description=(
            "`queued` -> `running` -> `succeeded`, `failed` or `cancelled`. A running job "
            "whose process died is marked `failed`; jobs are not retried."
        ),
    )
    params: dict[str, Any] = Field(
        default_factory=dict, description="The request body the job was submitted with.",
    )
    result: dict[str, Any] | None = Field(
        default=None,
        description="On success: the body the synchronous call returns.",
    )
    error: JobError | None = Field(
        None, description="On failure: why; null otherwise.",
    )
    progress: JobProgress | None = Field(
        None, description="How much is done, for a kind that reports it; null otherwise.",
    )
    consumer: str | None = Field(default=None, description="Who submitted it (token consumer).")
    cancel_requested: bool = Field(
        False, description="Someone asked to cancel it (`POST /v1/jobs/{id}:cancel`).",
    )
    sync: bool = Field(
        default=False,
        description=(
            "Run by a synchronous call: its caller waits for the response. Listed so "
            "others see it running; it can be cancelled like a background job."
        ),
    )
    created_at: datetime = Field(..., description="When it was submitted.")
    started_at: datetime | None = Field(
        None, description="When it started running; null while queued.",
    )
    finished_at: datetime | None = Field(
        None,
        description=(
            "When it finished; null until then. Finished jobs are deleted after "
            "`GRAPHOS_JOB_RETENTION_H` hours."
        ),
    )


class JobList(BaseModel):
    """Jobs, newest first."""

    jobs: list[Job] = Field(
        default_factory=list, description="At most `limit` jobs, by `created_at` descending.",
    )

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

    status: int
    detail: str


class Job(BaseModel):
    id: UUID
    kind: str = Field(
        ..., description=(
            "`recompute_communities`, `compute_metrics`, `resolve_edges`, `check_properties` "
            "or `vacuum`."
        ),
    )
    tenant: str | None = Field(
        default=None, description="Null for instance-wide jobs (vacuum).",
    )
    status: JobStatus
    params: dict[str, Any] = Field(
        default_factory=dict, description="The request body the job was submitted with.",
    )
    result: dict[str, Any] | None = Field(
        default=None,
        description="On success: the body the synchronous call returns.",
    )
    error: JobError | None = None
    consumer: str | None = Field(default=None, description="Who submitted it (token consumer).")
    cancel_requested: bool = False
    sync: bool = Field(
        default=False,
        description=(
            "Run by a synchronous call: its caller waits for the response. Listed so "
            "others see it running; it can be cancelled like a background job."
        ),
    )
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobList(BaseModel):
    jobs: list[Job] = Field(default_factory=list)

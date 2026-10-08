"""Contracts for the change feed: ``/v1/{tenant}/changes``."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

DEFAULT_CHANGES_LIMIT = 100
MAX_CHANGES_LIMIT = 1000


class ChangeItem(BaseModel):
    """One write: what it changed, not the new values (re-read those)."""

    cursor: str = Field(..., description="This record's position; pass it as `after`.")
    at: datetime = Field(..., description="Start time of the writing transaction.")
    op: Literal["upsert", "merge", "patch", "delete", "resolve", "communities", "metrics"]
    kind: Literal["node", "edge"]
    label: str | None = Field(None, description="The node label or edge type.")
    src_label: str | None = None
    dst_label: str | None = None
    ids: list[str] | None = Field(
        None, description="Node ids written (node records); null for a summary record.",
    )
    edges: list[tuple[str, str]] | None = Field(
        None, description="`[src_id, dst_id]` of the edges written (edge records).",
    )
    rev: int | None = Field(None, description="The new revision, for a single-item write.")
    count: int = Field(..., description="Nodes or edges this record covers.")
    detail: dict[str, Any] | None = Field(
        None,
        description=(
            "Per-op detail: `edges_removed` (edges deleted with the nodes, for the whole "
            "statement) and `where` on node deletes."
        ),
    )
    consumer: str | None = Field(None, description="Token consumer that made the write.")
    trace_id: str | None = None


class ChangesResponse(BaseModel):
    tenant: str
    changes: list[ChangeItem]
    next: str = Field(..., description="Pass as `after` for the next page.")
    more: bool = Field(..., description="The page was full: more records may be ready now.")


class ChangesHead(BaseModel):
    tenant: str
    cursor: str = Field(
        ..., description="A position before every change not yet visible: start a sync here.",
    )

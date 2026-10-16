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
    op: Literal[
        "upsert", "merge", "patch", "delete", "resolve", "communities", "metrics", "schema",
    ] = Field(
        ...,
        description=(
            "`schema`: the tenant's schema changed (kind `tenant`; `detail` says how). "
            "New values may be added: treat an unknown op as informational."
        ),
    )
    kind: Literal["node", "edge", "tenant"] = Field(
        ...,
        description=(
            "What was written: `node`, `edge`, or `tenant` (the schema, with op `schema`)."
        ),
    )
    label: str | None = Field(None, description="The node label or edge type.")
    src_label: str | None = Field(
        None, description="Edge records: the source node's label; null when not known.",
    )
    dst_label: str | None = Field(
        None, description="Edge records: the target node's label; null when not known.",
    )
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
            "statement) and `where` on node deletes; for `schema`, what was added, removed "
            "or switched."
        ),
    )
    consumer: str | None = Field(None, description="Token consumer that made the write.")
    trace_id: str | None = Field(
        None,
        description=(
            "The trace id of the request that made the write (from its `traceparent` header), "
            "or `job:<id>` for a background job; null when the request had none."
        ),
    )


class ChangesResponse(BaseModel):
    """A page of the change feed, oldest first."""

    tenant: str = Field(..., description="The tenant.")
    changes: list[ChangeItem] = Field(..., description="The records after `after`, in feed order.")
    next: str = Field(..., description="Pass as `after` for the next page.")
    more: bool = Field(..., description="The page was full: more records may be ready now.")


class ChangesHistory(BaseModel):
    """A page of the change feed read backwards, newest first."""

    tenant: str = Field(..., description="The tenant.")
    changes: list[ChangeItem] = Field(..., description="Newest first.")
    older: str | None = Field(
        None, description="Pass as `before` for the page before this one; null at the end.",
    )
    more: bool = Field(..., description="The page was full: older records may exist.")


class ChangesHead(BaseModel):
    """The position to start following the feed from."""

    tenant: str = Field(..., description="The tenant.")
    cursor: str = Field(
        ..., description="A position before every change not yet visible: start a sync here.",
    )

"""Pydantic contracts for snapshot-import.

Snapshot import takes one NDJSON stream holding both nodes and edges and
writes them in label-grouped chunks through the same COPY-staged SQL upserts
as the other write endpoints (see ``api/snapshot.py``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# Rows merged per chunk.  Each chunk commits on its own: smaller chunks mean
# more transactions but less work lost on a failure part-way.
DEFAULT_SNAPSHOT_MERGE_CHUNK = 5000
MAX_SNAPSHOT_ROWS = 50_000_000   # 50M rows per stream; larger streams get 413


class SnapshotImportResponse(BaseModel):
    """What a snapshot import read, wrote and refused."""

    tenant: str = Field(..., description="The tenant.")
    nodes_received: int = Field(
        0, description="Node lines read (a line without `_kind` is a node), invalid ones included.",
    )
    edges_received: int = Field(0, description="Edge lines read, invalid ones included.")
    nodes_upserted: int = Field(0, description="Nodes written: inserted, or merged into.")
    edges_upserted: int = Field(0, description="Edges written: inserted, or merged into.")
    edges_skipped: int = Field(
        0, description="Valid edge lines not written because an end node does not exist.",
    )
    by_node_label: dict[str, int] = Field(
        default_factory=dict, description="Nodes written per vertex label.",
    )
    by_edge_type: dict[str, int] = Field(
        default_factory=dict, description="Edges written per edge type.",
    )
    copy_ms: int = Field(
        0,
        description=(
            "Time reading the stream, in milliseconds, with the chunks written on the way "
            "(every 5000 rows of a label or type)."
        ),
    )
    merge_ms: int = Field(
        0, description="Time writing the last, partly filled chunks, in milliseconds.",
    )
    elapsed_ms: int = Field(0, description="Total time, in milliseconds.")
    errors: list[str] = Field(
        default_factory=list,
        description=(
            "Why lines were not written, as `line <n>: <reason>` (not JSON, invalid, an "
            "unregistered label or type, a property violation); the first 50."
        ),
    )
    mode: Literal["merge", "empty"] = Field(
        "merge",
        description=(
            "The `mode` used: `merge` (into what the tenant holds) or `empty` (into a tenant "
            "without nodes)."
        ),
    )
    header: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The file's header line (format, version, source tenant, when, counts), without "
            "its schema; null for a file without one."
        ),
    )
    complete: bool | None = Field(
        default=None,
        description=(
            "Whether the file ended with its `end` line: false means it was cut off. "
            "Null for a file without a header."
        ),
    )
    edge_communities_set: int = Field(
        default=0,
        description="Edge rows whose ends' communities were restored from the nodes'.",
    )
    property_violations: dict[str, int] = Field(
        default_factory=dict,
        description="Refused rows' property errors, by `<label or *>.<property>:<rule>` (strict).",
    )
    property_warnings: dict[str, int] = Field(
        default_factory=dict,
        description="Written rows' property problems, by `<label or *>.<property>:<rule>`.",
    )

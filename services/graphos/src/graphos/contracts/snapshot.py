"""Pydantic contracts for snapshot-import.

Snapshot import takes one NDJSON stream holding both nodes and edges and
writes them in label-grouped chunks through the same COPY-staged SQL upserts
as the other write endpoints (see ``api/snapshot.py``).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Rows merged per chunk.  Each chunk commits on its own: smaller chunks mean
# more transactions but less work lost on a failure part-way.
DEFAULT_SNAPSHOT_MERGE_CHUNK = 5000
MAX_SNAPSHOT_ROWS = 50_000_000   # 50M rows per stream; larger streams get 413


class SnapshotImportResponse(BaseModel):
    tenant: str
    nodes_received: int = 0
    edges_received: int = 0
    nodes_upserted: int = 0
    edges_upserted: int = 0
    edges_skipped: int = 0
    by_node_label: dict[str, int] = Field(default_factory=dict)
    by_edge_type: dict[str, int] = Field(default_factory=dict)
    copy_ms: int = 0
    merge_ms: int = 0
    elapsed_ms: int = 0
    errors: list[str] = Field(default_factory=list)

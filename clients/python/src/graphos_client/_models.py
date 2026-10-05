"""Wire-shape models mirrored from graphos's service-side contracts.

We keep these as a parallel copy rather than importing from the graphos
service package because:

- The client is published / distributed separately from the service.
- Consumers shouldn't have a runtime dep on `graphos` (which pulls
  in asyncpg, igraph, fastapi, …).

Field shapes match `graphos.contracts.graph` and friends 1:1. When the
service contract changes, this file is updated alongside.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# ── Nodes / Edges (write) ───────────────────────────────────────────────────


class NodeUpsert(BaseModel):
    id: str
    label: str
    properties: dict[str, object] = Field(default_factory=dict)


class EdgeUpsert(BaseModel):
    src_id: str
    src_label: str
    dst_id: str
    dst_label: str
    type: str
    properties: dict[str, object] = Field(default_factory=dict)


# ── Tenant ──────────────────────────────────────────────────────────────────


class TenantInfo(BaseModel):
    name: str
    vertex_labels: list[str] = Field(default_factory=list)
    edge_labels: list[str] = Field(default_factory=list)
    n_indexes: int = 0
    n_generated_columns: int = 0
    meta: dict[str, str] = Field(default_factory=dict)


# ── Reads ───────────────────────────────────────────────────────────────────


class NodeHit(BaseModel):
    id: int
    label: str
    properties: dict[str, object] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    tenant: str
    query: str
    label: str | None = None
    limit: int
    truncated: bool
    elapsed_ms: int
    nodes: list[NodeHit] = Field(default_factory=list)


class EdgeHit(BaseModel):
    id: int
    src_id: str
    dst_id: str
    type: str
    properties: dict[str, object] = Field(default_factory=dict)


class Neighbor(BaseModel):
    neighbor_id: str
    neighbor_label: str
    neighbor_properties: dict[str, object] = Field(default_factory=dict)
    direction: str
    edge_type: str
    edge_properties: dict[str, object] = Field(default_factory=dict)


class NeighborsResponse(BaseModel):
    tenant: str
    node_id: str
    limit: int
    truncated: bool
    elapsed_ms: int
    neighbors: list[Neighbor] = Field(default_factory=list)


class InducedEdgesResponse(BaseModel):
    tenant: str
    node_count: int
    rel_types: list[str] | None = None
    limit: int
    truncated: bool
    elapsed_ms: int
    edges: list[EdgeHit] = Field(default_factory=list)


class PathHit(BaseModel):
    end_id: str
    end_label: str
    end_properties: dict[str, object] = Field(default_factory=dict)
    hops: int
    rel_path: list[str] = Field(default_factory=list)


class ExpandResponse(BaseModel):
    tenant: str
    node_id: str
    max_hops: int
    direction: str
    rel_types: list[str] | None = None
    limit: int
    truncated: bool
    elapsed_ms: int
    paths: list[PathHit] = Field(default_factory=list)


class AggregateRow(BaseModel):
    src_value: str
    dst_value: str
    rel_type: str
    count: int


class EdgesAggregateResponse(BaseModel):
    tenant: str
    group_by_src: str
    group_by_dst: str
    rel_types: list[str] | None = None
    truncated: bool
    elapsed_ms: int
    groups: list[AggregateRow] = Field(default_factory=list)


class NodeAggregateRow(BaseModel):
    value: str
    count: int


class NodesAggregateResponse(BaseModel):
    tenant: str
    label: str
    group_by: str
    truncated: bool
    elapsed_ms: int
    groups: list[NodeAggregateRow] = Field(default_factory=list)


# ── Sample subgraph (graph-viz fixture) ───────────────────────────────────


class SampleNodeHit(BaseModel):
    id: str
    properties: dict[str, object] = Field(default_factory=dict)


class SampleEdgeHit(BaseModel):
    src_id: str
    dst_id: str
    type: str
    properties: dict[str, object] = Field(default_factory=dict)


class SampleSubgraphResponse(BaseModel):
    tenant: str
    label: str
    rel_types: list[str]
    per_rel: int
    truncated: bool
    elapsed_ms: int
    nodes: list[SampleNodeHit] = Field(default_factory=list)
    edges: list[SampleEdgeHit] = Field(default_factory=list)


# ── Write responses ─────────────────────────────────────────────────────────


class NodesUpsertResponse(BaseModel):
    tenant: str
    nodes_upserted: int
    by_label: dict[str, int] = Field(default_factory=dict)
    elapsed_ms: int


class EdgesUpsertResponse(BaseModel):
    tenant: str
    edges_upserted: int
    edges_skipped: int
    by_type: dict[str, int] = Field(default_factory=dict)
    elapsed_ms: int


class BulkNodesResponse(BaseModel):
    tenant: str
    rows_received: int
    rows_invalid: int
    nodes_upserted: int
    by_label: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    elapsed_ms: int


class BulkEdgesResponse(BaseModel):
    tenant: str
    rows_received: int
    rows_invalid: int
    edges_upserted: int
    edges_skipped: int
    by_type: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    elapsed_ms: int


# ── Delete responses ────────────────────────────────────────────────────────


class DeleteNodeResponse(BaseModel):
    tenant: str
    node_id: str
    nodes_deleted: int
    edges_deleted: int
    elapsed_ms: int


class DeleteByPropertyResponse(BaseModel):
    tenant: str
    property_key: str
    property_value: str
    nodes_deleted: int
    edges_deleted: int
    elapsed_ms: int


# ── Bulk node enumeration (/nodes:scan) ────────────────────────────────────


class NodesScanItem(BaseModel):
    """One node row in a /nodes:scan response."""

    id: str
    label: str
    properties: dict[str, Any] = Field(default_factory=dict)


class NodesScanResponse(BaseModel):
    """Response shape for POST /v1/{tenant}/graph/nodes:scan.

    ``next_cursor`` is opaque — round-trip it back unchanged to fetch
    the next page; ``None`` means the scan is fully drained.
    """

    tenant: str
    labels: list[str]
    items: list[NodesScanItem] = Field(default_factory=list)
    next_cursor: str | None = None
    scanned: int
    truncated_by_payload_cap: bool = False
    elapsed_ms: int


class EdgesScanItem(BaseModel):
    """One edge row in a /edges:scan response — mirrors EdgeUpsert."""

    src_id: str
    src_label: str
    dst_id: str
    dst_label: str
    type: str
    properties: dict[str, Any] = Field(default_factory=dict)


class EdgesScanResponse(BaseModel):
    """Response shape for POST /v1/{tenant}/graph/edges:scan.

    ``next_cursor`` is opaque — round-trip back unchanged.
    """

    tenant: str
    types: list[str]
    items: list[EdgesScanItem] = Field(default_factory=list)
    next_cursor: str | None = None
    scanned: int
    truncated_by_payload_cap: bool = False
    elapsed_ms: int


# ── Louvain ─────────────────────────────────────────────────────────────────


class RecomputeCommunitiesResponse(BaseModel):
    tenant: str
    n_nodes: int
    n_edges_scanned: int
    n_communities: int
    n_assigned: int
    n_singletons: int
    largest_community_size: int
    modularity: float
    edge_stream_ms: int
    igraph_ms: int
    writeback_ms: int
    elapsed_ms: int
    dry_run: bool


# ── Background jobs ─────────────────────────────────────────────────────────


class JobError(BaseModel):
    """The HTTP status and detail the synchronous call would have returned."""

    status: int
    detail: str


class Job(BaseModel):
    """A background job (``submit_*`` methods, ``GET /v1/jobs/{id}``)."""

    id: UUID
    kind: str
    tenant: str | None = None
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    params: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: JobError | None = None
    consumer: str | None = None
    cancel_requested: bool = False
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def done(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled")


# ── Community reads + tenant stats ─────────────────────────────────────────


class CommunityRow(BaseModel):
    community_id: int
    size: int
    sample_labels: list[str] = Field(default_factory=list)
    member_ids: list[str] | None = None


class CommunitiesResponse(BaseModel):
    tenant: str
    min_size: int
    n_communities: int
    n_assigned_nodes: int
    n_unassigned_nodes: int
    elapsed_ms: int
    communities: list[CommunityRow] = Field(default_factory=list)


class CommunitySummary(BaseModel):
    tenant: str
    n_communities: int
    n_assigned_nodes: int
    n_unassigned_nodes: int
    largest_community_size: int
    smallest_community_size: int
    top: list[CommunityRow] = Field(default_factory=list)
    elapsed_ms: int


class CommunityIdsResponse(BaseModel):
    """Response shape for the batched external id → community_id lookup.

    ``truncated`` is true when more ids were sent than the server's
    per-request cap and only the first ones were looked up.
    """

    tenant: str
    n_requested: int
    n_resolved: int
    community_by_id: dict[str, int] = Field(default_factory=dict)
    truncated: bool = False
    elapsed_ms: int


# ── Generic analytics primitives ─────────────────────────────────────


class HistogramRow(BaseModel):
    value: str
    count: int


class HistogramResponse(BaseModel):
    tenant: str
    label: str
    property: str
    n_distinct: int
    n_total: int
    bins: list[HistogramRow] = Field(default_factory=list)
    elapsed_ms: int


class TopByEdgeRow(BaseModel):
    node_id: str
    edge_count: int


class TopByEdgeResponse(BaseModel):
    tenant: str
    label: str
    by_edge: str
    direction: str
    rows: list[TopByEdgeRow] = Field(default_factory=list)
    elapsed_ms: int


class NodePropertiesUpsertResponse(BaseModel):
    tenant: str
    external_id: str
    label: str
    properties_merged: int
    elapsed_ms: int


class NodeGetResponse(BaseModel):
    tenant: str
    external_id: str
    label: str
    properties: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: int


class EdgeGetResponse(BaseModel):
    """One edge, named by its type and the ids of its two ends."""

    tenant: str
    type: str
    src_id: str
    src_label: str
    dst_id: str
    dst_label: str
    properties: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: int


class DeleteEdgeResponse(BaseModel):
    tenant: str
    type: str
    src_id: str
    dst_id: str
    edges_deleted: int
    elapsed_ms: int


# ── Schema-agnostic K/V cache ────────────────────────────────────────


class CachePutResponse(BaseModel):
    tenant: str
    key: str
    expires_at: datetime | None = None
    bytes_written: int
    elapsed_ms: int


class CacheGetResponse(BaseModel):
    tenant: str
    key: str
    value: Any = None
    expires_at: datetime | None = None
    cached_at: datetime
    source: str  # "lru" | "pg"
    elapsed_ms: int


class LabelCount(BaseModel):
    label: str
    kind: str
    count: int
    total_bytes: int = 0


class TenantStatsResponse(BaseModel):
    tenant: str
    n_vertices: int
    n_edges: int
    total_bytes: int
    labels: list[LabelCount] = Field(default_factory=list)
    elapsed_ms: int


# ── Export (graphology) ────────────────────────────────────────────────────


class GraphologyNode(BaseModel):
    key: str
    attributes: dict[str, object] = Field(default_factory=dict)


class GraphologyEdge(BaseModel):
    key: str
    source: str
    target: str
    undirected: bool = False
    attributes: dict[str, object] = Field(default_factory=dict)


class GraphologyExport(BaseModel):
    attributes: dict[str, object] = Field(default_factory=dict)
    options: dict[str, object] = Field(default_factory=dict)
    nodes: list[GraphologyNode] = Field(default_factory=list)
    edges: list[GraphologyEdge] = Field(default_factory=list)

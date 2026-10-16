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


class NodeRef(BaseModel):
    """One node by its label and id, as :meth:`GraphosClient.delete_nodes` takes it."""

    id: str
    label: str


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
    # Declared ``{label, properties, kind, unique, name}`` and
    # ``{label, column, property, type}``; empty from a server before 0.6.0.
    property_indexes: list[dict[str, Any]] = Field(default_factory=list)
    generated_columns: list[dict[str, Any]] = Field(default_factory=list)
    n_resolve_rules: int = 0
    label_hierarchy: dict[str, str] = Field(default_factory=dict)
    meta: dict[str, str] = Field(default_factory=dict)
    search_indexes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # ``{"vertex": {label: {color, caption, size_by, icon}}, "edge": {type: {color, caption}}}``.
    display: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    # ``{"mode", "vertex": {label: entry}, "edge": {type: entry}}``; empty from an older server.
    property_schema: dict[str, Any] = Field(default_factory=dict)


class Me(BaseModel):
    """``GET /v1/me``: who is signed in and what they may use."""

    consumer: str
    kind: str = "person"                      # person | service | anonymous
    via: str = "password"                     # password | oidc | none: how it signed in
    admin: bool = False
    must_change: bool = False                 # the password must change first
    tenants: list[str] = Field(default_factory=list)
    levels: dict[str, str] = Field(default_factory=dict)   # tenant -> read|write|maintain|owner
    create_tenants: list[str] = Field(default_factory=list)  # name patterns
    preferences: dict[str, Any] = Field(default_factory=dict)  # the web UI's, per user


class UnregisteredTenant(BaseModel):
    """A tenant unregistered with its data kept (``GET /v1/tenants?unregistered=true``).

    ``kind`` is ``unregistered`` (``restore_tenant`` brings it back as it was) or
    ``orphan`` (a graph unregistered before 0.8.2, with no kept registration:
    admin only).
    """

    name: str
    kind: str = "unregistered"
    vertex_labels: int = 0
    edge_labels: int = 0
    created_at: datetime | None = None
    unregistered_at: datetime | None = None
    unregistered_by: str | None = None


class CypherResult(BaseModel):
    """What a Cypher console query returned (``POST /v1/{tenant}/cypher``).

    ``rows`` holds one list of cells per row: a vertex as
    ``{"node": {id, label, properties}}``, an edge as ``{"edge": {type,
    properties, src_id, src_label, dst_id, dst_label}}``, a path as a list of
    them.  ``graph`` holds the returned nodes and the edges between them.
    """

    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    graph: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    truncated: bool = False
    elapsed_ms: int = 0


class EdgeEndpoint(BaseModel):
    """One label pair an edge type is declared to join: ``src`` -> ``dst``."""

    src: str
    dst: str


class TenantSchemaInfo(BaseModel):
    name: str
    vertex_labels: list[str] = Field(default_factory=list)
    edge_labels: list[str] = Field(default_factory=list)
    label_hierarchy: dict[str, str] = Field(default_factory=dict)
    search_indexes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rule_edge_labels: list[str] = Field(default_factory=list)
    # Empty and False from a server that predates declared endpoints.
    edge_endpoints: dict[str, list[EdgeEndpoint]] = Field(default_factory=dict)
    strict_endpoints: bool = False
    # How the web UI draws labels and edge types; empty from an older server.
    display: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    # Declared properties per label and edge type, and the mode; empty from an older server.
    property_schema: dict[str, Any] = Field(default_factory=dict)


# ── Reads ───────────────────────────────────────────────────────────────────


class MapLabel(BaseModel):
    label: str
    count: int


class MapLinkType(BaseModel):
    type: str
    count: int


class MapLink(BaseModel):
    src_label: str
    type: str
    dst_label: str
    count: int
    estimated: bool = False


class GraphMap(BaseModel):
    """``GET …/graph/map``: labels with node counts, edge counts between labels."""

    tenant: str
    labels: list[MapLabel] = Field(default_factory=list)
    links: list[MapLink] = Field(default_factory=list)
    estimated: bool = False
    elapsed_ms: int = 0


class HubNode(BaseModel):
    id: str
    label: str
    properties: dict[str, Any] = Field(default_factory=dict)
    degree: int
    estimated: bool = False


class HubsResponse(BaseModel):
    """``GET …/graph/map/hubs``: a label's best-connected nodes."""

    tenant: str
    label: str
    nodes: list[HubNode] = Field(default_factory=list)
    elapsed_ms: int = 0


class CommunityHub(BaseModel):
    id: str
    label: str
    properties: dict[str, Any] = Field(default_factory=dict)
    degree: int


class CommunityNode(BaseModel):
    community_id: int
    size: int
    labels: dict[str, int] = Field(default_factory=dict)
    hub: CommunityHub | None = None
    internal_edges: int = 0


class CommunityLink(BaseModel):
    src: int
    dst: int
    count: int
    types: list[MapLinkType] = Field(default_factory=list)


class CommunityMap(BaseModel):
    """``GET …/graph/communities/map``: the last recompute's communities and
    the edges between them."""

    tenant: str
    available: bool
    computed_at: datetime | None = None
    modularity: float | None = None
    n_communities: int = 0
    communities: list[CommunityNode] = Field(default_factory=list)
    links: list[CommunityLink] = Field(default_factory=list)


class NodeHit(BaseModel):
    id: int
    label: str
    properties: dict[str, object] = Field(default_factory=dict)


class SearchHit(NodeHit):
    score: float | None = None      # set when a search index answered
    matched: str | None = None      # the property that matched best


class SearchResponse(BaseModel):
    tenant: str
    query: str
    label: str | None = None
    index: str | None = None
    limit: int
    truncated: bool
    elapsed_ms: int
    nodes: list[SearchHit] = Field(default_factory=list)


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
    total: int | None = None
    distinct: int | None = None
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
    property_warnings: dict[str, int] = Field(default_factory=dict)


class EdgesUpsertResponse(BaseModel):
    tenant: str
    edges_upserted: int
    edges_skipped: int
    by_type: dict[str, int] = Field(default_factory=dict)
    elapsed_ms: int
    property_warnings: dict[str, int] = Field(default_factory=dict)


class BulkNodesResponse(BaseModel):
    tenant: str
    rows_received: int
    rows_invalid: int
    nodes_upserted: int
    by_label: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    elapsed_ms: int
    property_violations: dict[str, int] = Field(default_factory=dict)
    property_warnings: dict[str, int] = Field(default_factory=dict)


class BulkEdgesResponse(BaseModel):
    tenant: str
    rows_received: int
    rows_invalid: int
    edges_upserted: int
    edges_skipped: int
    by_type: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    elapsed_ms: int
    property_violations: dict[str, int] = Field(default_factory=dict)
    property_warnings: dict[str, int] = Field(default_factory=dict)


class CsvColumnPlan(BaseModel):
    """One CSV column as it was imported: the property it filled, its type."""

    header: str
    property: str
    type: str


class CsvIssue(BaseModel):
    """One kind of problem in the file (``empty_id``, ``bad_value``,
    ``duplicate_id``, …), counted, with a few examples."""

    code: str
    refused: bool
    count: int
    examples: list[dict[str, Any]] = Field(default_factory=list)


class CsvImportResponse(BaseModel):
    """What ``nodes:csv`` / ``edges:csv`` did (or, in a dry run, would do)."""

    tenant: str
    dry_run: bool
    delimiter: str
    rows: int
    records: int
    rows_invalid: int
    written: int
    edges_skipped: int = 0
    columns: list[CsvColumnPlan] = Field(default_factory=list)
    issues: list[CsvIssue] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    elapsed_ms: int
    property_violations: dict[str, int] = Field(default_factory=dict)
    property_warnings: dict[str, int] = Field(default_factory=dict)


# ── Delete responses ────────────────────────────────────────────────────────


class DeleteNodeResponse(BaseModel):
    tenant: str
    node_id: str
    nodes_deleted: int
    edges_deleted: int
    elapsed_ms: int


class DeleteNodesResponse(BaseModel):
    tenant: str
    nodes_requested: int
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


class ComputeMetricsResponse(BaseModel):
    """``POST /v1/{tenant}/graph/metrics:compute``: what was computed and written."""

    tenant: str
    metrics: list[str]
    properties: list[str] = Field(default_factory=list)
    n_nodes: int
    n_edges_scanned: int
    n_components: int | None = None
    n_changed: int
    n_cleared: int
    betweenness_skipped: bool = False
    warnings: list[str] = Field(default_factory=list)
    edge_stream_ms: int
    compute_ms: int
    writeback_ms: int
    elapsed_ms: int
    dry_run: bool


class PropertyPolicy(BaseModel):
    """How one vertex label's or edge type's writes are checked."""

    mode: str
    undeclared: str
    undeclared_source: str
    properties: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PropertyDefinitionsResponse(BaseModel):
    """``GET /v1/{tenant}/graph/properties``: every label's and type's definitions."""

    tenant: str
    mode: str
    vertex: dict[str, PropertyPolicy] = Field(default_factory=dict)
    edge: dict[str, PropertyPolicy] = Field(default_factory=dict)


class LabelCheck(BaseModel):
    """What one vertex label's or edge type's stored rows break."""

    kind: str
    name: str
    rows: int
    rows_with_errors: int
    rows_with_warnings: int
    errors: dict[str, int] = Field(default_factory=dict)
    warnings: dict[str, int] = Field(default_factory=dict)
    examples: dict[str, list[str]] = Field(default_factory=dict)
    undeclared: dict[str, int] = Field(default_factory=dict)


class PropertiesCheckResponse(BaseModel):
    """``POST /v1/{tenant}/graph/properties:check``: the stored rows against the definitions."""

    tenant: str
    mode: str
    rows: int
    rows_with_errors: int
    rows_with_warnings: int
    labels: list[LabelCheck] = Field(default_factory=list)
    elapsed_ms: int


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
    sync: bool = False     # run by a synchronous call (its caller waits); listed while running
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
    rev: int | None = None    # None from a server older than 0.2
    etag: str | None = None   # send back as ``if_match`` to write only this version
    elapsed_ms: int


class NodeGetResponse(BaseModel):
    tenant: str
    external_id: str
    label: str
    properties: dict[str, Any] = Field(default_factory=dict)
    rev: int | None = None    # None from a server older than 0.2
    etag: str | None = None   # send back as ``if_match`` to write only this version
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
    rev: int | None = None    # None from a server older than 0.2
    etag: str | None = None   # send back as ``if_match`` to write only this version
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


# ── Shortest paths ───────────────────────────────────────────────────


class PathNode(BaseModel):
    id: str
    label: str
    properties: dict[str, Any] | None = None


class PathEdge(BaseModel):
    """An edge on a path, in its stored direction ``src_id`` → ``dst_id``."""

    type: str
    src_id: str
    dst_id: str
    properties: dict[str, Any] | None = None


class GraphPath(BaseModel):
    """``edges[i]`` joins ``nodes[i]`` and ``nodes[i + 1]``."""

    hops: int
    nodes: list[PathNode]
    edges: list[PathEdge]


class PathsResponse(BaseModel):
    tenant: str
    src: str
    dst: str
    direction: str
    hops: int | None    # None when no path was found
    paths: list[GraphPath]
    truncated: bool     # the search stopped at its edge cap: a path may exist
    visited: int
    elapsed_ms: int


# ── Change feed ──────────────────────────────────────────────────────


class ChangeItem(BaseModel):
    """One write: what it changed, not the new values (re-read those)."""

    cursor: str            # this record's position; pass as ``after``
    at: datetime
    op: str                # upsert | merge | patch | delete | resolve | communities | metrics
                           # | schema; more may come: treat an unknown op as informational
    kind: str              # node | edge | tenant (schema records)
    label: str | None = None
    src_label: str | None = None
    dst_label: str | None = None
    ids: list[str] | None = None                  # node records
    edges: list[tuple[str, str]] | None = None    # edge records: (src_id, dst_id)
    rev: int | None = None
    count: int
    detail: dict[str, Any] | None = None
    consumer: str | None = None
    trace_id: str | None = None


class ChangesPage(BaseModel):
    tenant: str
    changes: list[ChangeItem]
    next: str              # pass as ``after`` for the next page
    more: bool             # the page was full: more may be ready now


class ChangesHistory(BaseModel):
    tenant: str
    changes: list[ChangeItem]   # newest first
    older: str | None = None    # pass as ``before`` for the page before; None at the end
    more: bool

"""Pydantic contracts for community read endpoints + tenant stats."""

from __future__ import annotations

from pydantic import BaseModel, Field

# ── Communities list / summary ─────────────────────────────────────────────


class CommunityRow(BaseModel):
    """One community in a /communities response."""

    community_id: int
    size: int
    sample_labels: list[str] = Field(
        default_factory=list,
        description="Top labels among members, in descending frequency.",
    )
    member_ids: list[str] | None = Field(
        default=None,
        description="Only populated when `include_members=true`.",
    )


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
    top: list[CommunityRow] = Field(
        default_factory=list,
        description="Top-N communities by size (default N=10, capped).",
    )
    elapsed_ms: int


# ── Per-tenant graph stats ─────────────────────────────────────────────────


class LabelCount(BaseModel):
    label: str
    kind: str = Field(..., description="`v` for vertex, `e` for edge")
    count: int
    total_bytes: int = 0


class TenantStatsResponse(BaseModel):
    tenant: str
    n_vertices: int
    n_edges: int
    total_bytes: int
    labels: list[LabelCount] = Field(default_factory=list)
    elapsed_ms: int


# ── Batch community-id lookup ──────────────────────────────────────────────
#
# Closes a client-side N+1: without it a caller fires one ``/search`` per
# node (20-50 sequential HTTP round-trips per query).  This endpoint resolves
# a list of external ids to their Louvain community_ids in one round-trip via
# the per-label ``<Label>_id_unique`` btrees.


class CommunityIdsRequest(BaseModel):
    """Lookup community_id for a list of external node ids in one request."""

    ids: list[str] = Field(
        ..., min_length=1,
        description="External node ids (the `id` property set when the node was written).",
    )


class CommunityIdsResponse(BaseModel):
    tenant: str
    n_requested: int
    n_resolved: int = Field(
        ...,
        description=("Number of ids that found a community_id. "
                     "Missing ids — either unknown to graphos or unclustered "
                     "by Louvain — are simply absent from `community_by_id`."),
    )
    community_by_id: dict[str, int] = Field(
        default_factory=dict,
        description="Maps external id → community_id for every resolved node.",
    )
    truncated: bool = Field(
        default=False,
        description="True when more ids were sent than the per-request cap "
                    "and only the first ones were looked up.",
    )
    elapsed_ms: int


# ── Generic analytics primitives ───────────────────────────────────────────
#
# Schema-agnostic shape: callers parameterise label + property + edge.
# These collapse what would otherwise be N sequential Cypher queries
# into one SQL ``GROUP BY`` per call.  Useful for any
# consumer that needs "top-K nodes by edge degree" — not coupled to any
# consumer's data model.


class TopByEdgeRow(BaseModel):
    node_id: str = Field(
        ...,
        description="External id (``properties.id``) of the parent node.",
    )
    edge_count: int


class TopByEdgeResponse(BaseModel):
    tenant: str
    label: str
    by_edge: str
    direction: str
    rows: list[TopByEdgeRow] = Field(default_factory=list)
    elapsed_ms: int

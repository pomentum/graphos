"""Pydantic contracts for community read endpoints + tenant stats."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ── Communities list / summary ─────────────────────────────────────────────


class CommunityRow(BaseModel):
    """One community in a /communities response."""

    community_id: int = Field(
        ..., description="The `community_id` the last community recompute wrote on the members.",
    )
    size: int = Field(..., description="Nodes with this `community_id`.")
    sample_labels: list[str] = Field(
        default_factory=list,
        description="Labels among the members, in alphabetical order, the first 5.",
    )
    member_ids: list[str] | None = Field(
        default=None,
        description=(
            "Up to 500 member ids (nodes without an `id` are left out). Only populated when "
            "`include_members=true`; null otherwise."
        ),
    )


class CommunitiesResponse(BaseModel):
    """Communities from the last community recompute, largest first."""

    tenant: str = Field(..., description="The tenant.")
    min_size: int = Field(
        ..., description="The `min_size` applied: smaller communities are left out.",
    )
    n_communities: int = Field(
        ..., description="Communities in this answer, after `min_size` and `limit`.",
    )
    n_assigned_nodes: int = Field(
        ..., description="Nodes of the tenant with a `community_id`, over all communities.",
    )
    n_unassigned_nodes: int = Field(
        ..., description="Nodes of the tenant without a `community_id`.",
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")
    communities: list[CommunityRow] = Field(
        default_factory=list,
        description="The communities, by size descending, then `community_id`.",
    )


class CommunitySummary(BaseModel):
    """Totals over the last community recompute, and its largest communities."""

    tenant: str = Field(..., description="The tenant.")
    n_communities: int = Field(..., description="Distinct `community_id` values on the nodes.")
    n_assigned_nodes: int = Field(..., description="Nodes with a `community_id`.")
    n_unassigned_nodes: int = Field(..., description="Nodes without a `community_id`.")
    largest_community_size: int = Field(
        ..., description="Members of the largest community; 0 when there is none.",
    )
    smallest_community_size: int = Field(
        ..., description="Members of the smallest community; 0 when there is none.",
    )
    top: list[CommunityRow] = Field(
        default_factory=list,
        description="Top-N communities by size (`top`, default 10, at most 100).",
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")


# ── Per-tenant graph stats ─────────────────────────────────────────────────


class LabelCount(BaseModel):
    """One label's storage table: its rows and size."""

    label: str = Field(..., description="The vertex label or edge type.")
    kind: str = Field(..., description="`v` for vertex, `e` for edge")
    count: int = Field(
        ...,
        description=(
            "Exact rows of the label's table. A parent label's count includes its child "
            "labels' rows, so the counts can overlap."
        ),
    )
    total_bytes: int = Field(
        0,
        description=(
            "The label table's size on disk with its indexes and TOAST "
            "(`pg_total_relation_size`), in bytes."
        ),
    )


class AnalysisFreshness(BaseModel):
    """How current an analysis's stored results are."""

    computed_at: datetime = Field(description="When the last run wrote its results.")
    scope: dict[str, Any] | None = Field(
        None, description="`rel_types` / `node_filter` of that run; null when it read everything.",
    )
    rows_written_since: int | None = Field(
        ...,
        description=(
            "Nodes and edges written, patched or deleted since the run read the graph (from the "
            "change feed); null when those records are past the feed's retention."
        ),
    )
    at_least: bool = Field(
        False, description="The count stopped at 10,000 change records: there were more.",
    )
    stale: bool | None = Field(
        ...,
        description=(
            "Whether the graph changed since the run: `rows_written_since > 0`; null when "
            "unknown. A write racing the run counts as since, so a run is never called "
            "current by mistake."
        ),
    )


class TenantStatsResponse(BaseModel):
    """A tenant's graph size: node and edge counts, and storage by label."""

    tenant: str = Field(..., description="The tenant.")
    n_vertices: int = Field(..., description="Nodes in the graph, each counted once.")
    n_edges: int = Field(..., description="Edges in the graph, each counted once.")
    total_bytes: int = Field(..., description="The sum of `total_bytes` over `labels`, in bytes.")
    labels: list[LabelCount] = Field(
        default_factory=list, description="Every label, by `total_bytes` descending.",
    )
    analyses: dict[str, AnalysisFreshness] = Field(
        default_factory=dict,
        description=(
            "For `metrics` (`graph/metrics:compute`) and `communities` (Louvain or Leiden) "
            "that have written results: when, and how much the graph changed since. Absent "
            "until a run writes."
        ),
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")


# ── Batch community-id lookup ──────────────────────────────────────────────
#
# Closes a client-side N+1: without it a caller fires one ``/search`` per
# node (20-50 sequential HTTP round-trips per query).  This endpoint resolves
# a list of external ids to their community_ids in one round-trip via
# the per-label ``<Label>_id_unique`` btrees.


class CommunityIdsRequest(BaseModel):
    """Lookup community_id for a list of external node ids in one request."""

    ids: list[str] = Field(
        ..., min_length=1,
        description="External node ids (the `id` property set when the node was written).",
    )


class CommunityIdsResponse(BaseModel):
    """The `community_id` of each id looked up."""

    tenant: str = Field(..., description="The tenant.")
    n_requested: int = Field(..., description="Ids looked up: those sent, at most 500.")
    n_resolved: int = Field(
        ...,
        description=("Number of ids that found a community_id. "
                     "Missing ids — either unknown to graphos or unclustered "
                     "by the last recompute — are simply absent from `community_by_id`."),
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
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")


# ── Generic analytics primitives ───────────────────────────────────────────
#
# Schema-agnostic shape: callers parameterise label + property + edge.
# These collapse what would otherwise be N sequential Cypher queries
# into one SQL ``GROUP BY`` per call.  Useful for any
# consumer that needs "top-K nodes by edge degree" — not coupled to any
# consumer's data model.


class TopByEdgeRow(BaseModel):
    """One node and its edge count."""

    node_id: str = Field(
        ...,
        description="External id (``properties.id``) of the parent node.",
    )
    edge_count: int = Field(
        ...,
        description=(
            "Edges of type `by_edge` on the node in `direction`; with `both`, a self-loop "
            "counts twice."
        ),
    )


class TopByEdgeResponse(BaseModel):
    """The nodes of a label with the most edges of one type."""

    tenant: str = Field(..., description="The tenant.")
    label: str = Field(..., description="The vertex label counted.")
    by_edge: str = Field(..., description="The edge type counted.")
    direction: str = Field(
        ...,
        description="`out` (edges leaving the node), `in` (entering it) or `both`.",
    )
    rows: list[TopByEdgeRow] = Field(
        default_factory=list,
        description=(
            "At most `k` nodes, by `edge_count` descending, then `node_id`; nodes without such "
            "an edge are absent."
        ),
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")

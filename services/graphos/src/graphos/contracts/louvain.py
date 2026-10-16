"""Pydantic contracts for the Louvain admin endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from graphos.contracts.graph import _validate_prop_key

DEFAULT_LOUVAIN_RESOLUTION = 1.0
DEFAULT_LOUVAIN_MIN_COMMUNITY_SIZE = 3

# Each value may be a scalar (=, exact match) or a list (IN).
NodeFilterValue = str | int | float | bool | list[str | int | float | bool]


class RecomputeCommunitiesRequest(BaseModel):
    """A Louvain community recompute over the tenant's edges (`POST …/recompute-communities`)."""

    rel_types: list[str] | None = Field(
        default=None,
        description="Restrict the edge stream to these types. Default: all.",
    )
    rel_weights: dict[str, float] | None = Field(
        default=None,
        description=(
            "Per-edge-type weights for modularity. Default: 1.0 for every "
            "type. Useful when some rel types are noisier than others."
        ),
    )
    node_filter: dict[str, NodeFilterValue] | None = Field(
        default=None,
        description=(
            "Property-keyed predicates that BOTH endpoints of every edge "
            "must satisfy. Scalar value → `=` match; list → `IN`. Example: "
            "`{\"project_id\": \"<sid>\"}` runs Louvain over a single "
            "consumer-defined subgraph (the per-tenant scoped-recompute "
            "pattern). Multi-value: `{\"project_id\": [\"<sid_a>\", \"<sid_b>\"]}`."
        ),
    )
    resolution: float = Field(
        default=DEFAULT_LOUVAIN_RESOLUTION,
        gt=0.0,
        le=10.0,
        description="Louvain resolution. Higher = more, smaller communities.",
    )
    min_community_size: int = Field(
        default=DEFAULT_LOUVAIN_MIN_COMMUNITY_SIZE,
        ge=1,
        description="Drop communities smaller than this. Singletons go uncoloured.",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "If true, compute communities but DON'T write community_id back."
        ),
    )

    @field_validator("node_filter")
    @classmethod
    def _validate_node_filter_keys(
        cls, v: dict[str, NodeFilterValue] | None,
    ) -> dict[str, NodeFilterValue] | None:
        if v is None:
            return None
        for key in v:
            _validate_prop_key(key)   # raises ValueError on bad identifier
        return v


class RecomputeCommunitiesResponse(BaseModel):
    """What a Louvain recompute found and how long each phase took."""

    tenant: str = Field(..., description="The tenant.")
    n_nodes: int = Field(..., description="Nodes clustered: the ends of the edges read.")
    n_edges_scanned: int = Field(
        ..., description="Edges read: those of `rel_types` whose ends match `node_filter`.",
    )
    n_communities: int = Field(
        ..., description="Communities of at least `min_community_size` members.",
    )
    n_assigned: int = Field(
        ...,
        description=(
            "Nodes in those communities, given a `community_id` (unless `dry_run`; only the "
            "changed values are written)."
        ),
    )
    n_singletons: int = Field(
        ...,
        description=(
            "Groups Louvain found that are smaller than `min_community_size`; their nodes get "
            "no `community_id`. A count of groups, not of nodes."
        ),
    )
    largest_community_size: int = Field(
        ..., description="Members of the largest community; 0 when there is none.",
    )
    modularity: float = Field(
        ..., description="The partition's weighted modularity; 0 when no edge was read.",
    )
    edge_stream_ms: int = Field(..., description="Time reading the edges, in milliseconds.")
    igraph_ms: int = Field(..., description="Time running Louvain, in milliseconds.")
    writeback_ms: int = Field(
        ..., description="Time writing `community_id` and the community map, in milliseconds.",
    )
    elapsed_ms: int = Field(..., description="Total time, in milliseconds.")
    dry_run: bool = Field(..., description="Nothing was written: the request's `dry_run`.")

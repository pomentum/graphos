"""Contracts for the graph map: labels, the links between them, and hubs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MapLabel(BaseModel):
    """One vertex label and how many nodes it holds (its own rows, not its children's)."""

    label: str = Field(..., description="The vertex label.")
    count: int = Field(..., description="Nodes stored under this label itself; exact, above 0.")


class MapLinkType(BaseModel):
    """Edges of one type between two communities."""

    type: str = Field(..., description="The edge type.")
    count: int = Field(..., description="Edges of this type between the two communities.")


class MapLink(BaseModel):
    """Edges of one type from nodes of one label to nodes of another."""

    src_label: str = Field(..., description="The source nodes' label.")
    type: str = Field(..., description="The edge type.")
    dst_label: str = Field(..., description="The target nodes' label.")
    count: int = Field(..., description="Exact, or estimated from a sample when `estimated`.")
    estimated: bool = Field(
        False,
        description=(
            "The edge type's table holds over 300000 rows (planner estimate) and was sampled; "
            "never with `exact=true`."
        ),
    )


class GraphMapResponse(BaseModel):
    """The tenant at a glance: one entry per label, one per (label, type, label) link."""

    tenant: str = Field(..., description="The tenant.")
    labels: list[MapLabel] = Field(
        ..., description="Vertex labels holding nodes, by count descending, then name.",
    )
    links: list[MapLink] = Field(
        ...,
        description="Every (source label, type, target label) with edges, by count descending.",
    )
    estimated: bool = Field(
        ..., description="Some link counts were estimated from a sample of a large edge table.",
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")


class HubNode(BaseModel):
    """A node of the label with its number of edges (estimated when `estimated`)."""

    id: str = Field(
        ..., description="The node's external id; its graph id when it has no `id` property.",
    )
    label: str = Field(..., description="The vertex label asked for.")
    properties: dict[str, Any] = Field(..., description="The node's stored properties.")
    degree: int = Field(..., description="Its edges, in and out, of every type.")
    estimated: bool = Field(
        False,
        description="Part of `degree` comes from a sample of an edge table of over 300000 rows.",
    )


class HubsResponse(BaseModel):
    """A label's best-connected nodes."""

    tenant: str = Field(..., description="The tenant.")
    label: str = Field(..., description="The vertex label.")
    nodes: list[HubNode] = Field(
        ..., description="At most `limit` nodes, by degree descending; empty without edges.",
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")


class CommunityHub(BaseModel):
    """A community's best-connected member, to name the community after."""

    id: str = Field(
        ..., description="The node's external id; its graph id when it has no `id` property.",
    )
    label: str = Field(..., description="The node's vertex label.")
    properties: dict[str, Any] = Field(..., description="The node's current stored properties.")
    degree: int = Field(
        ...,
        description=(
            "Its edges, in and out, among those the recompute clustered (ties go to the lower "
            "graph id)."
        ),
    )


class CommunityNode(BaseModel):
    """One community as the last Louvain recompute stored it."""

    community_id: int = Field(..., description="The `community_id` written on its members.")
    size: int = Field(..., description="Its members.")
    labels: dict[str, int] = Field(..., description="Members per label, for up to 5 labels.")
    hub: CommunityHub | None = Field(
        None, description="Its best-connected member; null when that node no longer exists.",
    )
    internal_edges: int = Field(
        ..., description="Clustered edges with both ends in the community.",
    )


class CommunityLink(BaseModel):
    """Edges from members of one community to members of another."""

    src: int = Field(..., description="The source community's `community_id`.")
    dst: int = Field(..., description="The target community's `community_id`.")
    count: int = Field(..., description="Edges from `src` to `dst`, over every type.")
    types: list[MapLinkType] = Field(..., description="The edges per type, by count descending.")


class CommunityMapResponse(BaseModel):
    """The communities of the last Louvain recompute and the edges between them."""

    tenant: str = Field(..., description="The tenant.")
    available: bool = Field(..., description="False until a recompute has stored a map.")
    computed_at: datetime | None = Field(
        None, description="When the recompute stored the map; null when not `available`.",
    )
    modularity: float | None = Field(
        None, description="The recompute's modularity; null when not `available`.",
    )
    n_communities: int = Field(
        0, description="Communities the recompute stored, all of them; 0 when not `available`.",
    )
    communities: list[CommunityNode] = Field(
        default_factory=list,
        description="At most `limit` communities, by size descending, then `community_id`.",
    )
    links: list[CommunityLink] = Field(
        default_factory=list,
        description="Edges between the communities listed, by count descending.",
    )

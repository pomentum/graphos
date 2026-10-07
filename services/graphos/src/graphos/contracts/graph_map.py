"""Contracts for the graph map: labels, the links between them, and hubs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MapLabel(BaseModel):
    """One vertex label and how many nodes it holds (its own rows, not its children's)."""

    label: str
    count: int


class MapLinkType(BaseModel):
    type: str
    count: int


class MapLink(BaseModel):
    """Edges of one type from nodes of one label to nodes of another."""

    src_label: str
    type: str
    dst_label: str
    count: int = Field(..., description="Exact, or estimated from a sample when `estimated`.")
    estimated: bool = False


class GraphMapResponse(BaseModel):
    """The tenant at a glance: one entry per label, one per (label, type, label) link."""

    tenant: str
    labels: list[MapLabel]
    links: list[MapLink]
    estimated: bool = Field(
        ..., description="Some link counts were estimated from a sample of a large edge table.",
    )
    elapsed_ms: int


class HubNode(BaseModel):
    """A node of the label with its number of edges (estimated when `estimated`)."""

    id: str
    label: str
    properties: dict[str, Any]
    degree: int
    estimated: bool = False


class HubsResponse(BaseModel):
    tenant: str
    label: str
    nodes: list[HubNode]
    elapsed_ms: int


class CommunityHub(BaseModel):
    """A community's best-connected member, to name the community after."""

    id: str
    label: str
    properties: dict[str, Any]
    degree: int


class CommunityNode(BaseModel):
    community_id: int
    size: int
    labels: dict[str, int] = Field(..., description="Members per label, most first (up to 5).")
    hub: CommunityHub | None = None
    internal_edges: int


class CommunityLink(BaseModel):
    """Edges from members of one community to members of another."""

    src: int
    dst: int
    count: int
    types: list[MapLinkType]


class CommunityMapResponse(BaseModel):
    """The communities of the last Louvain recompute and the edges between them."""

    tenant: str
    available: bool = Field(..., description="False until a recompute has stored a map.")
    computed_at: datetime | None = None
    modularity: float | None = None
    n_communities: int = 0
    communities: list[CommunityNode] = Field(default_factory=list)
    links: list[CommunityLink] = Field(default_factory=list)

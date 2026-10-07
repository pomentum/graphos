"""Contracts for the graph map: labels, the links between them, and hubs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MapLabel(BaseModel):
    """One vertex label and how many nodes it holds (its own rows, not its children's)."""

    label: str
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

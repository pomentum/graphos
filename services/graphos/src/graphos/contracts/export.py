"""Pydantic contracts for the graph export endpoint.

The wire shape is graphology's native serialization
(https://graphology.github.io/serialization.html) so that sigma.js
consumers can do ``Graph.from(payload)`` with zero parsing or extra
JS dependencies.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

DEFAULT_EXPORT_NODE_LIMIT = 5000
MAX_EXPORT_NODE_LIMIT = 50_000


class GraphologyNode(BaseModel):
    """One node in the graphology serialization format."""

    key: str
    attributes: dict[str, object] = Field(default_factory=dict)


class GraphologyEdge(BaseModel):
    """One edge in the graphology serialization format."""

    key: str
    source: str
    target: str
    undirected: bool = False
    attributes: dict[str, object] = Field(default_factory=dict)


class GraphologyExport(BaseModel):
    """Full graphology serialization. Consumable by ``Graph.from()``."""

    attributes: dict[str, object] = Field(default_factory=dict)
    options: dict[str, object] = Field(
        default_factory=lambda: {"type": "directed", "multi": False, "allowSelfLoops": True},
    )
    nodes: list[GraphologyNode] = Field(default_factory=list)
    edges: list[GraphologyEdge] = Field(default_factory=list)

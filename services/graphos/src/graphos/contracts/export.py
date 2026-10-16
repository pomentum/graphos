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

    key: str = Field(..., description="The node's external id.")
    attributes: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "The node's properties, plus `label` (its `name` property, else its id: what "
            "sigma.js displays) and `_kind` (its vertex label)."
        ),
    )


class GraphologyEdge(BaseModel):
    """One edge in the graphology serialization format."""

    key: str = Field(..., description="`e_<graph id>`: unique in the export.")
    source: str = Field(..., description="The source node's external id.")
    target: str = Field(..., description="The target node's external id.")
    undirected: bool = Field(False, description="Always false: every edge is directed.")
    attributes: dict[str, object] = Field(
        default_factory=dict,
        description="The edge's properties, plus `label` and `_type` (both its edge type).",
    )


class GraphologyExport(BaseModel):
    """Full graphology serialization. Consumable by ``Graph.from()``."""

    attributes: dict[str, object] = Field(
        default_factory=dict, description="Graph-level attributes; always empty.",
    )
    options: dict[str, object] = Field(
        default_factory=lambda: {"type": "directed", "multi": False, "allowSelfLoops": True},
        description=(
            "graphology's graph options: always `type` `directed`, `multi` false, "
            "`allowSelfLoops` true."
        ),
    )
    nodes: list[GraphologyNode] = Field(
        default_factory=list,
        description=(
            "At most `limit_nodes` nodes with an `id` (only those in `node_ids` when given); "
            "empty when none match."
        ),
    )
    edges: list[GraphologyEdge] = Field(
        default_factory=list,
        description="Every edge whose two ends are among `nodes`, of `rel_types` when given.",
    )

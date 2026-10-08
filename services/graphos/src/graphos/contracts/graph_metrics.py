"""Contracts for graph metrics: ``POST /v1/{tenant}/graph/metrics:compute``."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from graphos.contracts.graph import _validate_prop_key
from graphos.contracts.louvain import NodeFilterValue

Metric = Literal["degree", "pagerank", "components", "betweenness"]

DEFAULT_METRICS: list[Metric] = ["degree", "pagerank", "components"]


class ComputeMetricsRequest(BaseModel):
    metrics: list[Metric] = Field(
        default_factory=lambda: list(DEFAULT_METRICS),
        min_length=1,
        description=(
            "What to compute. `degree` writes graphos_degree, graphos_in_degree and "
            "graphos_out_degree; `pagerank` graphos_pagerank; `components` "
            "graphos_component (weakly connected, 0 = the largest, ids kept across runs); "
            "`betweenness` graphos_betweenness (exact, normalised; skipped above "
            "GRAPHOS_BETWEENNESS_MAX_NODES nodes)."
        ),
    )
    rel_types: list[str] | None = Field(
        default=None, description="Count only edges of these types. Default: all.",
    )
    node_filter: dict[str, NodeFilterValue] | None = Field(
        default=None,
        description=(
            "Only edges whose both ends match every `{property: value | [values]}`, "
            "as for community recompute."
        ),
    )
    damping: float = Field(
        default=0.85, gt=0.0, lt=1.0, description="PageRank damping factor.",
    )
    dry_run: bool = Field(
        default=False, description="Compute and report, write nothing.",
    )

    @field_validator("metrics")
    @classmethod
    def _unique(cls, v: list[Metric]) -> list[Metric]:
        return list(dict.fromkeys(v))

    @field_validator("rel_types")
    @classmethod
    def _rel_types(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [_validate_prop_key(t) for t in v]

    @field_validator("node_filter")
    @classmethod
    def _filter_keys(
        cls, v: dict[str, NodeFilterValue] | None,
    ) -> dict[str, NodeFilterValue] | None:
        if v:
            for key in v:
                _validate_prop_key(key)
        return v


class ComputeMetricsResponse(BaseModel):
    tenant: str
    metrics: list[Metric]
    properties: list[str] = Field(description="The graphos_* properties computed.")
    n_nodes: int = Field(description="Nodes with at least one edge of the chosen types.")
    n_edges_scanned: int
    n_components: int | None = None
    n_changed: int = Field(description="Nodes whose values changed (written unless dry_run).")
    n_cleared: int = Field(
        description="Nodes that had values but no longer take part (values removed).",
    )
    betweenness_skipped: bool = False
    warnings: list[str] = Field(default_factory=list)
    edge_stream_ms: int
    compute_ms: int
    writeback_ms: int
    elapsed_ms: int
    dry_run: bool

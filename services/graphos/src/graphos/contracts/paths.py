"""Contracts for ``POST /v1/{tenant}/graph/paths``: shortest paths."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from graphos.contracts.graph import _NODE_ID_MAX_LEN, validate_label_optional

DEFAULT_PATH_HOPS = 4
MAX_PATH_HOPS = 8
MAX_PATHS = 100
# Edges one level of the search may touch before that end of it stops
# (both ends stopped: ``truncated``).
MAX_PATH_LEVEL_EDGES = 100_000


class PathsRequest(BaseModel):
    """The shortest paths between two nodes, named by their ids."""

    src: str = Field(
        ..., min_length=1, max_length=_NODE_ID_MAX_LEN, description="`id` of the start node.",
    )
    dst: str = Field(
        ..., min_length=1, max_length=_NODE_ID_MAX_LEN, description="`id` of the end node.",
    )
    src_label: str | None = Field(None, description="Needed only when `src` is in several labels.")
    dst_label: str | None = Field(None, description="Needed only when `dst` is in several labels.")
    rel_types: list[str] | None = Field(
        None, min_length=1, max_length=100, description="Edge types to walk; all when absent.",
    )
    direction: Literal["out", "in", "both"] = Field(
        "both",
        description=(
            "`out` follows edges the way they point (src → … → dst), `in` against it, "
            "`both` either way."
        ),
    )
    max_hops: int = Field(
        DEFAULT_PATH_HOPS, ge=1, le=MAX_PATH_HOPS,
        description="The longest path to look for, in edges (1 to 8).",
    )
    limit: int = Field(1, ge=1, le=MAX_PATHS, description="Shortest paths to return, at most.")
    include_properties: bool = Field(False, description="Return node and edge properties.")

    @field_validator("src_label", "dst_label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        return validate_label_optional(v)

    @field_validator("rel_types")
    @classmethod
    def _rel_types(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            for t in v:
                validate_label_optional(t)
        return v


class PathNode(BaseModel):
    """A node on a path."""

    id: str = Field(..., description="The node's `id`.")
    label: str = Field(..., description="The node's vertex label.")
    properties: dict[str, object] | None = Field(
        None, description="The node's properties; null unless `include_properties`.",
    )


class PathEdge(BaseModel):
    """An edge on a path, with its stored direction (``src_id`` → ``dst_id``),
    which can run against the path when ``direction`` is ``in`` or ``both``."""

    type: str = Field(..., description="The edge type.")
    src_id: str = Field(..., description="`id` of the node the edge starts from.")
    dst_id: str = Field(..., description="`id` of the node the edge points to.")
    properties: dict[str, object] | None = Field(
        None, description="The edge's properties; null unless `include_properties`.",
    )


class GraphPath(BaseModel):
    """``edges[i]`` joins ``nodes[i]`` and ``nodes[i + 1]``."""

    hops: int = Field(..., description="Edges on the path.")
    nodes: list[PathNode] = Field(..., description="The nodes in order, `src` first, `dst` last.")
    edges: list[PathEdge] = Field(..., description="The edges in order: one fewer than `nodes`.")


class PathsResponse(BaseModel):
    """The shortest paths found between two nodes."""

    tenant: str = Field(..., description="The tenant.")
    src: str = Field(..., description="`id` of the start node, as sent.")
    dst: str = Field(..., description="`id` of the end node, as sent.")
    direction: str = Field(..., description="The `direction` walked: `out`, `in` or `both`.")
    hops: int | None = Field(
        ..., description="Length of the shortest paths; null when none was found.",
    )
    paths: list[GraphPath] = Field(
        ...,
        description="Up to `limit` paths, all `hops` long; empty when none was found.",
    )
    truncated: bool = Field(
        ...,
        description=(
            f"Both ends of the search reached a level of more than {MAX_PATH_LEVEL_EDGES:,} "
            "edges and it stopped: a path may exist.  Narrow `rel_types` or `direction`."
        ),
    )
    visited: int = Field(..., description="Vertices the search reached.")
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")

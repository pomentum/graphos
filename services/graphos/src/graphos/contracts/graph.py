"""Pydantic wire contracts for the graph data plane."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_LABEL_PATTERN = re.compile(r"^[A-Z][A-Za-z0-9_]{0,62}$")
_NODE_ID_MAX_LEN = 512

DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 200

# Bulk-enumeration scan limits — see /nodes:scan.
DEFAULT_SCAN_LIMIT = 500
MAX_SCAN_LIMIT = 2000
# Soft response-payload ceiling: if the accumulated `items` payload
# crosses this many bytes mid-page, the scan stops early and the
# cursor is positioned at the last included row.  Belt-and-braces:
# the per-row size is bounded by the node properties shape, which is
# usually <1 KB but can spike on fat nodes (large manifest blobs).
SCAN_PAYLOAD_CAP_BYTES = 10 * 1024 * 1024  # 10 MiB

# Small-batch upsert cap. Above this, consumers must use the NDJSON-streaming
# bulk endpoints (not implemented yet). The cap protects against one client
# pinning a connection on a very large MERGE roundtrip.
MAX_NODES_UPSERT_BATCH = 1000
MAX_EDGES_UPSERT_BATCH = 1000

# Bulk endpoints: server-side chunk size for the streamed NDJSON path.
# Rows are accumulated up to this many per label before being flushed to
# graphos.writes. Tuneable; tradeoff is roundtrip count vs memory.
BULK_FLUSH_CHUNK = 500


class NodeHit(BaseModel):
    """One row in a graph search / read response."""

    id: int = Field(  # AGE graphid
        ..., description="The node's AGE graph id (internal), not its `id` property.",
    )
    label: str = Field(..., description="The node's own vertex label.")  # vertex label
    properties: dict[str, object] = Field(
        default_factory=dict, description="The node's stored properties.",
    )


class SearchHit(NodeHit):
    """A search result; ``score`` and ``matched`` are set when a search index answered."""

    score: float | None = Field(None, description="Relevance in [0, 1] (search index only).")
    matched: str | None = Field(None, description="The property that matched best.")


class SearchResponse(BaseModel):
    """Response for GET /v1/{tenant}/graph/search."""

    tenant: str = Field(..., description="The tenant.")
    query: str = Field(..., description="The query `q`, as sent.")
    label: str | None = Field(None, description="The `label` searched; null for every label.")
    index: str | None = Field(None, description="The search index used; null for substring.")
    limit: int = Field(..., description="The `limit` applied.")
    truncated: bool = Field(
        ...,
        description=(
            "True iff the result set hit the limit; client may want to paginate "
            "or tighten the query. Backed by `count(rows) == limit`, not a "
            "round-trip second query — so it's a hint, not authoritative."
        ),
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")
    nodes: list[SearchHit] = Field(
        default_factory=list,
        description=(
            "The hits: by score with a search index; in no set order for a substring search."
        ),
    )


def validate_label_optional(value: str | None) -> str | None:
    """Re-used by query-param dependencies that accept an optional ``label``."""
    if value is None:
        return None
    if not _LABEL_PATTERN.fullmatch(value):
        raise ValueError(
            f"label '{value}' must match {_LABEL_PATTERN.pattern} (UpperCamelCase)"
        )
    return value


class _LabelHolder(BaseModel):
    """Internal helper to reuse the validator from a Pydantic context (tests)."""

    label: str | None = None

    @field_validator("label")
    @classmethod
    def _validate_label(cls, v: str | None) -> str | None:
        return validate_label_optional(v)


# ── Upsert ─────────────────────────────────────────────────────────────────


class NodeUpsert(BaseModel):
    """One row in a POST /v1/{tenant}/graph/nodes payload.

    The ``id`` is **consumer-assigned** — it's the property the MERGE clause
    keys on, not AGE's internal numeric id. Stable across re-runs of the
    same consumer-side scan.
    """

    id: str = Field(
        ..., min_length=1, max_length=_NODE_ID_MAX_LEN,
        description="The node's external id (1 to 512 characters): the key it is upserted on.",
    )
    label: str = Field(..., description="Vertex label; must be registered for the tenant.")
    properties: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "Free-form properties. `id` is set automatically from the top-level "
            "`id` field; you may include it here too, but the top-level value wins."
        ),
    )

    @field_validator("label")
    @classmethod
    def _validate_label(cls, v: str) -> str:
        if not _LABEL_PATTERN.fullmatch(v):
            raise ValueError(
                f"label '{v}' must match {_LABEL_PATTERN.pattern} (UpperCamelCase)"
            )
        return v


class NodesUpsertRequest(BaseModel):
    """Body for POST /v1/{tenant}/graph/nodes — small-batch upsert.

    Idempotent: re-POSTing the same payload is a no-op (graph state is the
    same); changed properties get merged additively (existing keys not in
    the new payload are preserved, openCypher ``SET n += map`` semantics).
    """

    nodes: list[NodeUpsert] = Field(
        ...,
        min_length=1,
        max_length=MAX_NODES_UPSERT_BATCH,
        description=(
            f"Up to {MAX_NODES_UPSERT_BATCH} nodes. For larger batches, use "
            "the NDJSON bulk endpoints (`/nodes:bulk`, `/edges:bulk`)."
        ),
    )


class NodesUpsertResponse(BaseModel):
    """What a small-batch node upsert wrote."""

    tenant: str = Field(..., description="The tenant.")
    nodes_upserted: int = Field(
        ...,
        description=(
            "Distinct nodes inserted or merged into (an id repeated in one label counts once)."
        ),
    )
    by_label: dict[str, int] = Field(
        default_factory=dict, description="`nodes_upserted` per vertex label.",
    )
    elapsed_ms: int = Field(..., description="Time spent writing, in milliseconds.")
    property_warnings: dict[str, int] = Field(
        default_factory=dict,
        description="Written rows' property problems, by `<label or *>.<property>:<rule>`.",
    )


# ── Single-node property merge ─────────────────────────────────────────────
#
# Generic "stamp these properties on the node identified by external id"
# primitive.  Schema-agnostic: one surface every consumer can use to upsert
# aggregates onto any metadata-bearing node (``:Team``, ``:Collection``,
# or whatever a consumer schema defines).


class NodePropertiesUpsertRequest(BaseModel):
    """Merge a property bag onto the node whose ``properties.id`` matches the
    URL ``external_id``.

    Merge semantics are last-write-wins per top-level key (Postgres ``agtype``
    ``||`` operator).  Keys not in ``properties`` are preserved; keys in
    ``properties`` overwrite their previous value.  To clear a key entirely,
    set it to ``null`` in ``properties`` and the value will be replaced by
    the agtype null sentinel (consumers should treat null as "absent").
    """

    properties: dict[str, object] = Field(
        ..., min_length=1,
        description="The property bag to merge onto the node.",
    )


class NodePropertiesUpsertResponse(BaseModel):
    """The node a property merge wrote to, and its new revision."""

    tenant: str = Field(..., description="The tenant.")
    external_id: str = Field(..., description="The node's `id`, as in the URL.")
    label: str = Field(
        ..., description=(
            "The AGE label of the node that was updated.  Reported so callers "
            "can verify they hit the metadata node they expected (cheap "
            "defence against id collisions across labels — a :Team id "
            "shouldn't accidentally hit a :Document row)."
        ),
    )
    properties_merged: int = Field(
        ..., description="Number of top-level keys merged from the payload.",
    )
    rev: int = Field(..., description="Revision: 1 when created, +1 on every write.")
    etag: str = Field(
        ..., description="Also the `ETag` header; send it as `If-Match` to write this version.",
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")


class NodeGetResponse(BaseModel):
    """Single-node read by external id, returning the full property bag."""

    tenant: str = Field(..., description="The tenant.")
    external_id: str = Field(..., description="The node's `id`, as in the URL.")
    label: str = Field(..., description="The node's own vertex label.")
    properties: dict[str, object] = Field(
        default_factory=dict, description="Every stored property, `id` included.",
    )
    rev: int = Field(..., description="Revision: 1 when created, +1 on every write.")
    etag: str = Field(
        ..., description="Also the `ETag` header; send it as `If-Match` to write this version.",
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")


# ── One edge, by identity ──────────────────────────────────────────────────


class EdgeGetResponse(BaseModel):
    """One edge, named by its type and the ids of its two ends."""

    tenant: str = Field(..., description="The tenant.")
    type: str = Field(..., description="The edge type.")
    src_id: str = Field(..., description="`id` of the source node, as asked.")
    src_label: str = Field(..., description="The source node's vertex label.")
    dst_id: str = Field(..., description="`id` of the target node, as asked.")
    dst_label: str = Field(..., description="The target node's vertex label.")
    properties: dict[str, object] = Field(
        default_factory=dict, description="The edge's stored properties.",
    )
    rev: int = Field(..., description="Revision: 1 when created, +1 on every write.")
    etag: str = Field(
        ..., description="Also the `ETag` header; send it as `If-Match` to write this version.",
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")


class DeleteEdgeResponse(BaseModel):
    """What deleting one edge removed; its end nodes stay."""

    tenant: str = Field(..., description="The tenant.")
    type: str = Field(..., description="The edge type.")
    src_id: str = Field(..., description="`id` of the source node, as asked.")
    dst_id: str = Field(..., description="`id` of the target node, as asked.")
    edges_deleted: int = Field(..., description="1, or 0 when there was no such edge.")
    elapsed_ms: int = Field(..., description="Time spent deleting, in milliseconds.")


# ── Many edges checked at once (POST /edges:check) ─────────────────────────


class EdgeCheckItem(BaseModel):
    """One edge to look for, named as in ``GET /edges/{type}``."""

    type: str = Field(..., description="Edge type; must be registered for the tenant.")
    src_id: str = Field(
        ..., min_length=1, max_length=_NODE_ID_MAX_LEN, description="`id` of the source node.",
    )
    dst_id: str = Field(
        ..., min_length=1, max_length=_NODE_ID_MAX_LEN, description="`id` of the target node.",
    )
    src_label: str | None = Field(
        None, description="Source label; needed only when the id is in several labels.",
    )
    dst_label: str | None = Field(
        None, description="Target label; needed only when the id is in several labels.",
    )

    @field_validator("type", "src_label", "dst_label")
    @classmethod
    def _validate_label(cls, v: str | None) -> str | None:
        if v is not None and not _LABEL_PATTERN.fullmatch(v):
            raise ValueError(
                f"label '{v}' must match {_LABEL_PATTERN.pattern} (UpperCamelCase / SNAKE_CASE)"
            )
        return v


class EdgesCheckRequest(BaseModel):
    """Body for POST /v1/{tenant}/graph/edges:check."""

    edges: list[EdgeCheckItem] = Field(
        ...,
        min_length=1,
        max_length=MAX_NODES_UPSERT_BATCH,
        description=f"Up to {MAX_NODES_UPSERT_BATCH} edges, each by type and the ids of its ends.",
    )
    include_properties: bool = Field(
        False, description="Return each present edge's properties too.",
    )


class EdgeCheckResult(BaseModel):
    """What was found for one requested edge, in request order."""

    type: str = Field(..., description="The edge type, as requested.")
    src_id: str = Field(..., description="`id` of the source node, as requested.")
    dst_id: str = Field(..., description="`id` of the target node, as requested.")
    status: Literal["present", "absent", "ambiguous"] = Field(
        ...,
        description=(
            "`present`: the edge exists. `absent`: no such edge, or an end does not exist. "
            "`ambiguous`: an end id is in several labels and more than one edge matches; "
            "`candidates` lists the label pairs, and `src_label` / `dst_label` pick one."
        ),
    )
    src_label: str | None = Field(
        None, description="The source's label: found when present, else as requested.",
    )
    dst_label: str | None = Field(
        None, description="The target's label: found when present, else as requested.",
    )
    rev: int | None = Field(None, description="The present edge's revision.")
    etag: str | None = Field(None, description="The present edge's ETag, for a later `If-Match`.")
    properties: dict[str, object] | None = Field(
        None, description="The present edge's properties, when `include_properties` is set.",
    )
    candidates: list[str] | None = Field(
        None, description="For `ambiguous`: the `<src label>-><dst label>` pairs that match.",
    )


class EdgesCheckResponse(BaseModel):
    """Response for POST /v1/{tenant}/graph/edges:check."""

    tenant: str = Field(..., description="The tenant.")
    results: list[EdgeCheckResult] = Field(..., description="One per requested edge, in order.")
    present: int = Field(..., description="Results with status `present`.")
    absent: int = Field(..., description="Results with status `absent`.")
    ambiguous: int = Field(..., description="Results with status `ambiguous`.")
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")


# ── Edge upsert ────────────────────────────────────────────────────────────


class EdgeUpsert(BaseModel):
    """One row in a POST /v1/{tenant}/graph/edges payload.

    Endpoints are addressed by ``(label, id)`` pairs. Both labels and the
    edge ``type`` must be registered for the tenant. Endpoint **nodes must
    already exist** — graphos does not auto-create endpoints; if either
    endpoint is missing, the edge is silently skipped and counted in
    ``edges_skipped`` so the consumer can detect ordering issues.
    """

    src_id: str = Field(
        ..., min_length=1, max_length=_NODE_ID_MAX_LEN,
        description="`id` of the source node, which must exist.",
    )
    src_label: str = Field(..., description="Vertex label of the source.")
    dst_id: str = Field(
        ..., min_length=1, max_length=_NODE_ID_MAX_LEN,
        description="`id` of the destination node, which must exist.",
    )
    dst_label: str = Field(..., description="Vertex label of the destination.")
    type: str = Field(..., description="Edge label / relationship type.")
    properties: dict[str, object] = Field(
        default_factory=dict,
        description="Free-form properties, merged into the edge's (keys not sent are kept).",
    )

    @field_validator("src_label", "dst_label", "type")
    @classmethod
    def _validate_label(cls, v: str) -> str:
        if not _LABEL_PATTERN.fullmatch(v):
            raise ValueError(
                f"label '{v}' must match {_LABEL_PATTERN.pattern} (UpperCamelCase / SNAKE_CASE)"
            )
        return v


class EdgesUpsertRequest(BaseModel):
    """Body for POST /v1/{tenant}/graph/edges — small-batch upsert.

    Idempotent on ``(src_id, dst_id, type)`` — re-POSTing the same payload
    is a graph-state no-op; props merge additively (openCypher
    ``SET r += map`` semantics).
    """

    edges: list[EdgeUpsert] = Field(
        ...,
        min_length=1,
        max_length=MAX_EDGES_UPSERT_BATCH,
        description=(
            f"Up to {MAX_EDGES_UPSERT_BATCH} edges. For larger batches, use "
            "the NDJSON bulk endpoints (`/nodes:bulk`, `/edges:bulk`)."
        ),
    )


class EdgesUpsertResponse(BaseModel):
    """What a small-batch edge upsert wrote and skipped."""

    tenant: str = Field(..., description="The tenant.")
    edges_upserted: int = Field(..., description="MERGEs whose MATCH found both endpoints.")
    edges_skipped: int = Field(..., description="Rows whose src or dst node didn't exist.")
    by_type: dict[str, int] = Field(
        default_factory=dict, description="`edges_upserted` per edge type.",
    )
    elapsed_ms: int = Field(..., description="Time spent writing, in milliseconds.")
    property_warnings: dict[str, int] = Field(
        default_factory=dict,
        description="Written rows' property problems, by `<label or *>.<property>:<rule>`.",
    )


# ── Bulk responses ─────────────────────────────────────────────────────────


class BulkNodesResponse(BaseModel):
    """Response for POST /v1/{tenant}/graph/nodes:bulk."""

    tenant: str = Field(..., description="The tenant.")
    rows_received: int = Field(..., description="Non-blank lines read, invalid ones included.")
    rows_invalid: int = Field(
        ...,
        description=(
            "Lines refused: not JSON or not a valid node, an unregistered label, a reserved "
            "property name, or a property error under `strict`; their line numbers appear in "
            "`errors`."
        ),
    )
    nodes_upserted: int = Field(
        ..., description="Nodes inserted or merged into, over every flushed batch.",
    )
    by_label: dict[str, int] = Field(
        default_factory=dict, description="`nodes_upserted` per vertex label.",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="The first 50 invalid lines' errors, in the form 'line N: <message>'.",
    )
    elapsed_ms: int = Field(..., description="Time spent reading and writing, in milliseconds.")
    property_violations: dict[str, int] = Field(
        default_factory=dict,
        description="Refused rows' property errors, by `<label or *>.<property>:<rule>` (strict).",
    )
    property_warnings: dict[str, int] = Field(
        default_factory=dict,
        description="Written rows' property problems, by `<label or *>.<property>:<rule>`.",
    )


class BulkEdgesResponse(BaseModel):
    """Response for POST /v1/{tenant}/graph/edges:bulk."""

    tenant: str = Field(..., description="The tenant.")
    rows_received: int = Field(..., description="Non-blank lines read, invalid ones included.")
    rows_invalid: int = Field(
        ...,
        description=(
            "Lines refused: not JSON or not a valid edge, an unregistered label or type, a "
            "reserved property name, a label pair `strict_endpoints` refuses, or a property "
            "error under `strict`."
        ),
    )
    edges_upserted: int = Field(
        ..., description="Edges inserted or merged into, over every flushed batch.",
    )
    edges_skipped: int = Field(
        ..., description="Valid lines not written because an end node does not exist.",
    )
    by_type: dict[str, int] = Field(
        default_factory=dict, description="`edges_upserted` per edge type.",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="The first 50 invalid lines' errors, in the form 'line N: <message>'.",
    )
    elapsed_ms: int = Field(..., description="Time spent reading and writing, in milliseconds.")
    property_violations: dict[str, int] = Field(
        default_factory=dict,
        description="Refused rows' property errors, by `<label or *>.<property>:<rule>` (strict).",
    )
    property_warnings: dict[str, int] = Field(
        default_factory=dict,
        description="Written rows' property problems, by `<label or *>.<property>:<rule>`.",
    )


# ── CSV import (POST /nodes:csv, /edges:csv) ──────────────────────────────


class CsvColumnPlan(BaseModel):
    """One CSV column as it was imported: the property it filled, its type."""

    header: str = Field(..., description="The column's header in the file.")
    property: str = Field(..., description="The property it fills.")
    type: Literal["auto", "text", "number", "boolean"] = Field(
        ...,
        description=(
            "How its cells are read: `auto` (`true` / `false`, plain numbers, else text), "
            "`text`, `number` or `boolean` (also `yes` / `no` / `1` / `0`)."
        ),
    )


class CsvIssue(BaseModel):
    """One kind of problem found in the file, counted, with a few examples."""

    code: str = Field(
        ...,
        description=(
            "`empty_id`, `long_id` (refused); `ragged` (a row of another length than the header), "
            "`bad_value` (a cell that does not read as its column's type, left out), "
            "`duplicate_id` / `duplicate_pair` (rows merged into one), `undeclared_pair`."
        ),
    )
    refused: bool = Field(..., description="The rows it names are not written.")
    count: int = Field(..., description="How many times it occurs in the file.")
    examples: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Up to 3: the row (from 1, under the header), column, value.",
    )


class CsvImportResponse(BaseModel):
    """Response for POST /v1/{tenant}/graph/nodes:csv and /edges:csv."""

    tenant: str = Field(..., description="The tenant.")
    dry_run: bool = Field(..., description="Checked only: nothing was written.")
    delimiter: Literal["comma", "semicolon", "tab"] = Field(
        ...,
        description=(
            "The separator used: as asked, else the one that splits the first lines most evenly."
        ),
    )
    rows: int = Field(..., description="Rows under the header (blank lines aside).")
    records: int = Field(
        ...,
        description="Nodes (or relationships) the rows make once rows repeating an id are merged.",
    )
    rows_invalid: int = Field(..., description="Records refused; their errors are in `errors`.")
    written: int = Field(
        ..., description="Nodes or relationships upserted (in a dry run: that would be).",
    )
    edges_skipped: int = Field(
        0, description="Relationships not written because an end does not exist.",
    )
    columns: list[CsvColumnPlan] = Field(
        default_factory=list, description="The property columns, as the mapping resolved them.",
    )
    issues: list[CsvIssue] = Field(default_factory=list, description="Refusals first.")
    errors: list[str] = Field(
        default_factory=list,
        description="The first 50 refused records, as 'row N: <message>' (the first of its rows).",
    )
    elapsed_ms: int = Field(..., description="Time spent, in milliseconds.")
    property_violations: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Refused records' property errors, by `<label or *>.<property>:<rule>` (strict)."
        ),
    )
    property_warnings: dict[str, int] = Field(
        default_factory=dict,
        description="Written records' property problems, by `<label or *>.<property>:<rule>`.",
    )


# ── Reads: neighbors + induced_edges ───────────────────────────────────────


DEFAULT_NEIGHBORS_LIMIT = 50
MAX_NEIGHBORS_LIMIT = 500

DEFAULT_INDUCED_LIMIT = 500
MAX_INDUCED_LIMIT = 5000
MAX_INDUCED_NODE_IDS = 1000

DEFAULT_EXPAND_HOPS = 2
MAX_EXPAND_HOPS = 5
DEFAULT_EXPAND_LIMIT = 100
MAX_EXPAND_LIMIT = 1000


class EdgeHit(BaseModel):
    """One edge row in a graph-read response."""

    id: int = Field(  # AGE relationship graphid
        ..., description="The edge's AGE graph id (internal).",
    )
    src_id: str = Field(  # neighbor's consumer-assigned `id` (or this node's)
        ..., description="`id` of the source node.",
    )
    dst_id: str = Field(..., description="`id` of the target node.")
    type: str = Field(..., description="The edge type.")
    properties: dict[str, object] = Field(
        default_factory=dict,
        description="The edge's properties; empty in a scoped subgraph, which does not read them.",
    )


class Neighbor(BaseModel):
    """One row in a /graph/neighbors response.

    `direction` says which way the edge points relative to the node passed
    in the request: ``out`` = edge from us to the neighbor, ``in`` = from
    neighbor to us.
    """

    neighbor_id: str = Field(
        ..., description="The neighbor's `id`; its graph id when it has none.",
    )
    neighbor_label: str = Field(..., description="The neighbor's vertex label.")
    neighbor_properties: dict[str, object] = Field(
        default_factory=dict, description="The neighbor's stored properties.",
    )
    direction: str = Field(
        ..., pattern="^(in|out)$",
        description="`out`: the edge points from the node to the neighbor; `in`: the other way.",
    )
    edge_type: str = Field(..., description="The connecting edge's type.")
    edge_properties: dict[str, object] = Field(
        default_factory=dict, description="The connecting edge's properties.",
    )


class NeighborsResponse(BaseModel):
    """A node's neighbors, one row per connecting edge."""

    tenant: str = Field(..., description="The tenant.")
    node_id: str = Field(..., description="`id` of the node, as sent.")
    limit: int = Field(..., description="The `limit` applied.")
    truncated: bool = Field(
        ..., description="`limit` rows were returned: there may be more (a hint).",
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")
    neighbors: list[Neighbor] = Field(
        default_factory=list,
        description=(
            "Outgoing edges first, then incoming; empty when the node does not exist."
        ),
    )


class InducedEdgesRequest(BaseModel):
    """Body for POST /v1/{tenant}/graph/induced_edges.

    Returns every edge whose ``src.id`` and ``dst.id`` are both in
    ``node_ids``. Optional ``rel_types`` narrows by edge type.
    """

    node_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=MAX_INDUCED_NODE_IDS,
        description="1 to 1000 node `id`s, in any label; unknown ids are ignored.",
    )
    rel_types: list[str] | None = Field(
        default=None,
        description="If set, restrict to edges of these types. Default: all types.",
    )
    limit: int = Field(
        default=DEFAULT_INDUCED_LIMIT, ge=1, le=MAX_INDUCED_LIMIT,
        description="Edges to return at most (1 to 5000).",
    )


class InducedEdgesResponse(BaseModel):
    """The edges among a set of nodes."""

    tenant: str = Field(..., description="The tenant.")
    node_count: int = Field(..., description="Entries in `node_ids`, as sent.")
    rel_types: list[str] | None = Field(
        None, description="The `rel_types` applied; null for every type.",
    )
    limit: int = Field(..., description="The `limit` applied.")
    truncated: bool = Field(
        ..., description="`limit` edges were returned: there may be more (a hint).",
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")
    edges: list[EdgeHit] = Field(
        default_factory=list, description="Edges whose two ends are both among the nodes.",
    )


class ScopedSubgraphNode(BaseModel):
    """One vertex in a scoped-subgraph response."""

    id: str = Field(..., description="The node's `id`; its graph id when it has none.")
    label: str = Field("", description="The node's own vertex label.")
    properties: dict[str, object] = Field(
        default_factory=dict,
        description="The `include_properties` the node has (null values left out).",
    )


class ScopedSubgraphRequest(BaseModel):
    """Body for POST /v1/{tenant}/graph/scoped_subgraph.

    Returns the vertices whose ``scope_property`` equals ``scope_value``,
    capped at ``node_cap``, plus the induced edges among them.  When
    ``group_property`` and ``group_values`` are given, only vertices whose
    group property is one of those values are kept, and the cap is shared
    evenly across the values so no single group swamps the result.
    ``exclude_property`` / ``exclude_substrings`` drop vertices whose
    property contains any of the (case-insensitive) substrings.

    graphos attaches no meaning to any of these names: the caller supplies
    every property.  The scope filter is one direct-SQL scan; declare an
    index on ``scope_property`` at registration for large tenants.
    """

    scope_property: str = Field(..., description="The property that selects the vertices.")
    scope_value: str = Field(
        ..., min_length=1,
        description="The value `scope_property` must equal: a string value, matched exactly.",
    )
    group_property: str | None = Field(
        None, description="A property to balance the vertices across; needs `group_values`.",
    )
    group_values: list[str] | None = Field(
        default=None, max_length=64,
        description=(
            "Keep only vertices whose `group_property` is one of these (up to 64), at most "
            "`node_cap / len(group_values)` (at least 40) of each."
        ),
    )
    include_properties: list[str] = Field(
        default_factory=lambda: ["name"], max_length=32,
        description="Properties returned on each vertex (up to 32; default `name`).",
    )
    exclude_property: str | None = Field(
        None, description="A property tested against `exclude_substrings`.",
    )
    exclude_substrings: list[str] | None = Field(
        default=None, max_length=64,
        description=(
            "Drop vertices whose `exclude_property` contains any of these, ignoring case "
            "(up to 64); needs `exclude_property`."
        ),
    )
    node_cap: int = Field(
        default=1200, ge=1, le=4000, description="Vertices to return at most (1 to 4000).",
    )
    rel_types: list[str] | None = Field(
        None, description="Edge types of the induced edges; all when absent.",
    )
    edge_limit: int = Field(
        default=8000, ge=1, le=12000, description="Edges to return at most (1 to 12000).",
    )

    @field_validator("scope_property", "group_property", "exclude_property")
    @classmethod
    def _validate_keys(cls, v: str | None) -> str | None:
        return None if v is None else _validate_prop_key(v)

    @field_validator("include_properties")
    @classmethod
    def _validate_include(cls, v: list[str]) -> list[str]:
        return [_validate_prop_key(k) for k in v]

    @model_validator(mode="after")
    def _check_pairs(self) -> ScopedSubgraphRequest:
        if self.group_values and not self.group_property:
            raise ValueError("group_values requires group_property")
        if self.exclude_substrings and not self.exclude_property:
            raise ValueError("exclude_substrings requires exclude_property")
        return self


class ScopedSubgraphResponse(BaseModel):
    """The vertices sharing one property value, and the edges among them."""

    tenant: str = Field(..., description="The tenant.")
    scope_property: str = Field(..., description="The `scope_property`, as sent.")
    scope_value: str = Field(..., description="The `scope_value`, as sent.")
    truncated: bool = Field(
        ...,
        description=(
            "`node_cap` was reached, or the scan read its row cap (4 x `node_cap`, between "
            "2000 and 8000): more vertices may match."
        ),
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")
    nodes: list[ScopedSubgraphNode] = Field(default_factory=list, description="The vertices.")
    edges: list[EdgeHit] = Field(
        default_factory=list,
        description="Edges with both ends among `nodes`, at most `edge_limit`, without properties.",
    )


class CatalogueResponse(BaseModel):
    """Resolver property→labels catalogue for one tenant.

    The catalogue is the in-process map the resolver compiler reads to
    decide which child-vertex tables carry a given property; it is
    rebuilt on tenant registration and on cache miss.  This endpoint
    exposes it as a diagnostic surface — given a resolve_rule's
    property names, an operator can verify what labels graphos
    actually considers candidates before firing ``/edges:resolve``.

    ``by_property`` keys are top-level JSONB property names found on
    at least one row in some child of ``_ag_label_vertex``; values
    are the leaf labels carrying them (inheritance parents are
    pruned, see ``catalogue._discover_label_parents``).
    """

    tenant: str = Field(..., description="The tenant.")
    by_property: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Per property name, the leaf vertex labels carrying it, sorted.",
    )


# ── Expand (variable-length BFS) ───────────────────────────────────────────


class PathHit(BaseModel):
    """One reachable endpoint with metadata about the path that got there."""

    end_id: str = Field(..., description="The reached node's `id`; its graph id when it has none.")
    end_label: str = Field(..., description="The reached node's vertex label.")
    end_properties: dict[str, object] = Field(
        default_factory=dict, description="The reached node's stored properties.",
    )
    hops: int = Field(..., ge=1, description="Edges from the nearest seed: the first hop found.")
    rel_path: list[str] = Field(
        ...,
        description="Edge types in order from seed to endpoint.",
    )


class ExpandResponse(BaseModel):
    """The nodes reached from the seeds, nearest first."""

    tenant: str = Field(..., description="The tenant.")
    node_id: str = Field(..., description="The `node_id`, as sent (also when `seeds` is used).")
    max_hops: int = Field(..., description="The `max_hops` applied.")
    direction: str = Field(..., description="The `direction` walked: `out`, `in` or `both`.")
    rel_types: list[str] | None = Field(
        None, description="The `rel_types` walked; null for every type.",
    )
    limit: int = Field(..., description="The `limit` applied.")
    truncated: bool = Field(
        ..., description="`limit` nodes were returned: more may be reachable (a hint).",
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")
    paths: list[PathHit] = Field(
        default_factory=list,
        description=(
            "One entry per node reached (seeds left out), by `hops`, then `end_id`; empty "
            "when no seed exists."
        ),
    )


# ── Edges aggregate ────────────────────────────────────────────────────────


_PROP_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

DEFAULT_AGGREGATE_LIMIT = 200
MAX_AGGREGATE_LIMIT = 5000


def _validate_prop_key(key: str) -> str:
    if not _PROP_KEY_PATTERN.fullmatch(key):
        raise ValueError(
            f"property key '{key}' must match {_PROP_KEY_PATTERN.pattern} — "
            "graphos disallows keys with whitespace, dots, or non-ascii to "
            "keep the Cypher template safely formattable."
        )
    return key


def validate_property_key(key: str) -> str:
    """A property name safe to put in SQL; raises ``ValueError`` otherwise."""
    return _validate_prop_key(key)


class EdgesAggregateRequest(BaseModel):
    """Body for POST /v1/{tenant}/graph/edges/aggregate.

    Groups edges by ``(src.<group_by_src>, dst.<group_by_dst>, type(r))`` and
    returns a count per group. Optionally filter on edge type, on values
    of the grouping property (whitelist), and on whether src and dst must
    differ on the grouping property (the "cross-group edges"
    case: same property name on both sides, distinct values required).
    """

    group_by_src: str = Field(..., description="Source-node property to group by.")
    group_by_dst: str = Field(..., description="Destination-node property to group by.")
    rel_types: list[str] | None = Field(
        default=None, description="Edge types to count; all when absent.",
    )
    src_values: list[str] | None = Field(
        default=None,
        description="If set, only count edges whose `src.<group_by_src>` is in this list.",
    )
    dst_values: list[str] | None = Field(
        default=None,
        description="If set, only count edges whose `dst.<group_by_dst>` is in this list.",
    )
    require_different: bool = Field(
        default=False,
        description="If true, only count edges where the two grouping values differ.",
    )
    limit: int = Field(
        default=DEFAULT_AGGREGATE_LIMIT, ge=1, le=MAX_AGGREGATE_LIMIT,
        description="Groups to return at most (1 to 5000).",
    )

    @field_validator("group_by_src", "group_by_dst")
    @classmethod
    def _validate_keys(cls, v: str) -> str:
        return _validate_prop_key(v)


class AggregateRow(BaseModel):
    """One group of edges and its count."""

    src_value: str = Field(
        ...,
        description="The source nodes' `group_by_src` value: a string as it is, else as JSON.",
    )
    dst_value: str = Field(
        ...,
        description="The target nodes' `group_by_dst` value: a string as it is, else as JSON.",
    )
    rel_type: str = Field(..., description="The edge type.")
    count: int = Field(
        ..., description="Edges in the group (both ends carry their grouping property).",
    )


class EdgesAggregateResponse(BaseModel):
    """Edges grouped by their ends' property values and type, with counts."""

    tenant: str = Field(..., description="The tenant.")
    group_by_src: str = Field(..., description="The `group_by_src`, as sent.")
    group_by_dst: str = Field(..., description="The `group_by_dst`, as sent.")
    rel_types: list[str] | None = Field(
        None, description="The `rel_types` counted; null for every type.",
    )
    truncated: bool = Field(..., description="There were more than `limit` groups.")
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")
    groups: list[AggregateRow] = Field(
        default_factory=list,
        description=(
            "At most `limit` groups, by count descending, then `src_value`, `dst_value`, "
            "`rel_type`."
        ),
    )


# ── Node aggregate (sibling of the edges variant) ─────────────────────────


class NodesAggregateRequest(BaseModel):
    """Body for POST /v1/{tenant}/graph/nodes/aggregate.

    Groups vertices of ``label`` by ``group_by`` (a single property name)
    and returns ``{value: <prop-value>, count: <n>}`` rows ordered by
    descending count.  Uses postgres table inheritance: passing the
    parent label ``Document`` matches all inheriting child labels
    (Article, Report, …) -- the same semantic as Cypher
    ``MATCH (n:Document)``.

    Useful for live summary views (e.g. counts per ``type`` or
    ``category``) when no materialised snapshot is available.  The
    Cypher template is ``MATCH (n:<label>) RETURN n.<group_by> AS v,
    count(n) AS c ORDER BY count(n) DESC LIMIT $lim``; ``label`` /
    ``group_by`` are regex-validated so the interpolation is safe.
    """

    label: str = Field(..., description="Vertex label to scan (parent label OK).")
    group_by: str = Field(..., description="Node property to group by.")
    values: list[str] | None = Field(
        default=None,
        description="If set, only count nodes whose `n.<group_by>` is in this list.",
    )
    limit: int = Field(
        default=DEFAULT_AGGREGATE_LIMIT, ge=1, le=MAX_AGGREGATE_LIMIT,
        description="Groups to return at most (1 to 5000).",
    )
    missing: bool = Field(
        default=False,
        description="Also count the nodes without `group_by`, under the value `\"\"`.",
    )

    @field_validator("label", "group_by")
    @classmethod
    def _validate_keys(cls, v: str) -> str:
        return _validate_prop_key(v)


class NodeAggregateRow(BaseModel):
    """One value of the property and how many nodes have it."""

    value: str = Field(
        ..., description="The property's value: a string as it is, any other value as JSON.",
    )
    count: int = Field(..., description="Nodes with this value.")


class NodesAggregateResponse(BaseModel):
    """Nodes of a label grouped by a property's value, with counts."""

    tenant: str = Field(..., description="The tenant.")
    label: str = Field(..., description="The `label` counted, its child labels included.")
    group_by: str = Field(..., description="The `group_by` property, as sent.")
    truncated: bool = Field(
        ..., description="`limit` groups were returned: compare with `distinct` for more.",
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")
    total: int = Field(..., description="Nodes counted in every group, `limit` aside.")
    distinct: int = Field(..., description="Groups (distinct values), `limit` aside.")
    groups: list[NodeAggregateRow] = Field(
        default_factory=list, description="At most `limit` groups, by count descending.",
    )


# ── Stratified sample-subgraph (for graph visualisation) ───────────────────


class SampleSubgraphRequest(BaseModel):
    """Body for POST /v1/{tenant}/graph/sample_subgraph.

    Returns a stratified edge sample -- up to ``per_rel`` edges per
    relationship type, taken from every edge type (or ``rel_types``) whatever
    the endpoint labels.  ``label`` selects the vertex table whose properties
    are read for the endpoints: ``include_properties`` are projected from it,
    and ``filter_property`` / ``filter_value`` keep only edges whose two
    endpoints are rows of that label with that value.  Endpoints outside the
    label come back as id-only nodes when no filter is set.

    The caller stays in control of every domain-specific knob -- which
    label is the "node of interest", which property scopes the query, which
    properties to materialise -- so this endpoint is reusable by any
    consumer of graphos, regardless of its data model.

    Stratification: a flat LIMIT over a graph where one rel type
    dominates (e.g. CITES at 90 %+) would render a one-colour graph
    in a visualisation.  Sampling ``per_rel`` per type guarantees every rel
    type the tenant has gets a visible footprint.  ``rel_types``
    optional whitelist short-circuits the histogram discovery when
    the caller already knows which types to include.
    """

    label: str = Field(
        ...,
        description=(
            "Vertex label whose properties are read for the sampled endpoints "
            "(parent label OK; inheritance applies). Edges are not filtered by it "
            "unless filter_property is set."
        ),
    )
    filter_property: str | None = Field(
        default=None,
        description="Optional property name to filter both endpoints by.",
    )
    filter_value: str | None = Field(
        default=None,
        description="Required when filter_property is set. Both endpoints must match.",
    )
    rel_types: list[str] | None = Field(
        default=None,
        description="Whitelist of edge types. If unset, discover via histogram.",
    )
    per_rel: int = Field(
        default=50, ge=1, le=500,
        description="Max edges per rel type. Stratifies the sample.",
    )
    include_properties: list[str] = Field(
        default_factory=list,
        description="Node properties to materialise on each returned hit. "
                    "Empty = return id only (lightweight).",
    )
    edge_properties: list[str] = Field(
        default_factory=list,
        description="Edge properties to materialise (e.g. 'confidence'). "
                    "Empty = return type only.",
    )

    @field_validator("label")
    @classmethod
    def _validate_label(cls, v: str) -> str:
        return _validate_prop_key(v)

    @field_validator("filter_property")
    @classmethod
    def _validate_filter_property(cls, v: str | None) -> str | None:
        return _validate_prop_key(v) if v else None


class SampleNodeHit(BaseModel):
    """One endpoint of the sampled edges."""

    id: str = Field(
        ..., description="The node's AGE graph id, as text: not its `id` property.",
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The `include_properties` it has, when it is a row of `label`; empty otherwise."
        ),
    )


class SampleEdgeHit(BaseModel):
    """One sampled edge."""

    src_id: str = Field(
        ..., description="The source node's AGE graph id, as text (a `nodes` entry's `id`).",
    )
    dst_id: str = Field(
        ..., description="The target node's AGE graph id, as text (a `nodes` entry's `id`).",
    )
    type: str = Field(..., description="The edge type.")
    properties: dict[str, Any] = Field(
        default_factory=dict, description="The `edge_properties` the edge has.",
    )


class SampleSubgraphResponse(BaseModel):
    """A sample of up to `per_rel` edges per type, and their ends."""

    tenant: str = Field(..., description="The tenant.")
    label: str = Field(..., description="The `label`, as sent.")
    rel_types: list[str] = Field(
        ..., description="The edge types sampled: as sent, else every edge type of the tenant.",
    )
    per_rel: int = Field(..., description="The `per_rel` applied.")
    truncated: bool = Field(..., description="Always false: `per_rel` is the only bound.")
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")
    nodes: list[SampleNodeHit] = Field(
        default_factory=list, description="The ends of the edges, each once.",
    )
    edges: list[SampleEdgeHit] = Field(
        default_factory=list,
        description=(
            "The sampled edges; with `filter_property`, only those whose two ends are rows "
            "of `label` with that value."
        ),
    )


# ── Delete responses ───────────────────────────────────────────────────────


class DeleteNodeResponse(BaseModel):
    """What deleting a node by id removed."""

    tenant: str = Field(..., description="The tenant.")
    node_id: str = Field(..., description="The node's `id`, as in the URL.")
    nodes_deleted: int = Field(
        ...,
        description=(
            "Nodes removed: every node with that id (in `label` only, when given); 0 when "
            "there was none."
        ),
    )
    edges_deleted: int = Field(
        ...,
        description="Edges incident to the deleted node, removed with it (each counted once).",
    )
    elapsed_ms: int = Field(..., description="Time spent deleting, in milliseconds.")


class NodeRef(BaseModel):
    """One node named by its label and consumer-assigned ``id``."""

    id: str = Field(
        ..., min_length=1, max_length=_NODE_ID_MAX_LEN,
        description="The node's external id (1 to 512 characters).",
    )
    label: str = Field(..., description="Vertex label; must be registered for the tenant.")

    @field_validator("label")
    @classmethod
    def _validate_label(cls, v: str) -> str:
        if not _LABEL_PATTERN.fullmatch(v):
            raise ValueError(
                f"label '{v}' must match {_LABEL_PATTERN.pattern} (UpperCamelCase)"
            )
        return v


class NodesDeleteRequest(BaseModel):
    """Body for POST /v1/{tenant}/graph/nodes:delete."""

    nodes: list[NodeRef] = Field(
        ...,
        min_length=1,
        max_length=MAX_NODES_UPSERT_BATCH,
        description=(
            f"Up to {MAX_NODES_UPSERT_BATCH} nodes, each by `label` and `id`. "
            "A node that does not exist is skipped."
        ),
    )


class DeleteNodesResponse(BaseModel):
    """Response for POST /v1/{tenant}/graph/nodes:delete."""

    tenant: str = Field(..., description="The tenant.")
    nodes_requested: int = Field(..., description="Entries in the request, duplicates included.")
    nodes_deleted: int = Field(
        ..., description="Nodes removed, child labels' nodes included; missing ones are skipped.",
    )
    edges_deleted: int = Field(
        ...,
        description="Edges incident to the deleted nodes, removed with them (each counted once).",
    )
    elapsed_ms: int = Field(..., description="Time spent deleting, in milliseconds.")


class DeleteByPropertyResponse(BaseModel):
    """Response for property-scoped tombstones (e.g. drop every node of one project)."""

    tenant: str = Field(..., description="The tenant.")
    property_key: str = Field(..., description="The `property_key`, as sent.")
    property_value: str = Field(..., description="The `property_value`, as sent.")
    nodes_deleted: int = Field(
        ...,
        description=(
            "Nodes removed: those whose property is the string `property_value` (not a number, "
            "not an array holding it)."
        ),
    )
    edges_deleted: int = Field(
        ..., description="Edges incident to the deleted nodes, removed with them (each once).",
    )
    elapsed_ms: int = Field(..., description="Time spent deleting, in milliseconds.")


# ── Bulk node enumeration (POST /nodes:scan) ──────────────────────────────


_SCAN_OPS = ("=", "!=", "in", "not in", "exists", "not exists")


class NodesScanWhere(BaseModel):
    """One AND-combined filter clause on POST /v1/{tenant}/graph/nodes:scan.

    Supported ``op`` values:

    * ``=`` / ``!=``     — scalar equality / inequality (string, number, bool).
    * ``in`` / ``not in`` — membership against a list ``value``.
    * ``exists`` / ``not exists`` — presence of the property key on the node.
      ``value`` is omitted for these.

    Substring / regex are intentionally NOT supported here — ``/search`` is
    the indexed text-matching primitive.  ``/scan`` is structural
    enumeration only.
    """

    property: str = Field(..., description="Property name to filter on.")
    op: Literal["=", "!=", "in", "not in", "exists", "not exists"] = Field(
        ...,
        description=(
            "`=` / `!=` compare with a scalar `value`; `in` / `not in` test membership in a list "
            "`value`; `exists` / `not exists` test whether the property is present."
        ),
    )
    value: Any | None = Field(
        default=None,
        description=(
            "Value for the comparison.  Required for `=`, `!=`, `in`, "
            "`not in`.  Omit for `exists` / `not exists`."
        ),
    )

    @field_validator("property")
    @classmethod
    def _validate_prop(cls, v: str) -> str:
        return _validate_prop_key(v)


class NodesScanOrderClause(BaseModel):
    """One key in a multi-key sort on POST /v1/{tenant}/graph/nodes:scan.

    ``property`` is a node-property name.  ``desc`` defaults to False
    (ascending).  The ``_node`` sentinel is NOT accepted in clause form —
    use the single-string ``order_by`` shape for graphid sort.
    """

    property: str = Field(..., description="Property name to sort on.")
    desc: bool = Field(
        default=False,
        description="True for descending sort on this key.",
    )

    @field_validator("property")
    @classmethod
    def _validate_prop(cls, v: str) -> str:
        if v == "_node":
            raise ValueError(
                "'_node' sentinel is only valid in the single-string "
                "order_by form; in clause form use a property name"
            )
        return _validate_prop_key(v)


class NodesScanRequest(BaseModel):
    """Body for POST /v1/{tenant}/graph/nodes:scan.

    Enumerate every node matching ``labels`` (1+, OR-combined) and
    ``where`` (AND-combined), with optional property projection and
    cursor pagination.  Schema-agnostic: graphos knows only label,
    property name, property value, external id.  The caller passes
    their own concepts (e.g. ``project_id``) as plain
    ``where`` clauses.

    Ordering is stable: by ``order_by`` within a label, with the node's
    graphid breaking ties.  When scanning multiple labels, label[0] is
    fully exhausted before moving to label[1].  Predictable + cheap to
    checkpoint.

    A label that is the parent of others in ``label_hierarchy`` includes
    its children's nodes, as every read on a parent does; each item
    carries its own leaf label.  A request (or a series of requests)
    naming both a parent and its child therefore returns the child's
    nodes twice, under the same ``(id, label)`` — de-duplicate on that
    pair.

    Page semantics: the response's ``next_cursor`` is opaque — round-
    trip it back in the next request without modification.  ``null``
    means the scan is fully drained.
    """

    labels: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "1+ vertex labels to scan, OR-combined.  All must be "
            "registered on the tenant."
        ),
    )
    where: list[NodesScanWhere] = Field(
        default_factory=list,
        description="AND-combined filters; empty list = no filter.",
    )
    project: list[str] | None = Field(
        default=None,
        description=(
            "Property names to return per node.  ``id`` is always "
            "implicitly included.  Pass ``['*']`` for the full "
            "property bag (bounded by the response payload cap).  "
            "Default ``None`` is equivalent to ``['id']``."
        ),
    )
    order_by: str | list[NodesScanOrderClause] | None = Field(
        default="id",
        description=(
            "Stable sort key(s) + cursor anchor.  Four forms:\n\n"
            "* **``None``**: skip ORDER BY entirely.  Use when the "
            "  caller does NOT paginate (always reads the whole "
            "  result in one request).  Eliminates the cost of "
            "  AGE-planner-induced wrong-index picks on the "
            "  ``<Label>_id_unique`` btree — empirically a "
            "  37 s → 235 ms win on large-label scans where "
            "  the WHERE clause has a sargable functional index on "
            "  another property.  Cursor pagination is incompatible "
            "  with ``order_by=None``; the route 422s in that case.\n"
            "* **Property name** (str, default ``id``): single-key ASC "
            "  sort on ``properties.<order_by>``.  ``id`` benefits "
            "  from graphos's auto-created ``<Label>_id_unique`` "
            "  btree.  Other property names need a matching "
            "  property index declared via ``POST /v1/tenants`` "
            "  ``property_indexes`` to stay index-driven on large tables.\n"
            "* **``_node`` sentinel** (str): single-key ASC sort by "
            "  the node's intrinsic identity (AGE ``graphid`` primary "
            "  key — always indexed).  Equivalent to Cypher's "
            "  ``ORDER BY n`` form.  Use when you want fast, stable "
            "  paging and don't care about the order value being "
            "  semantically meaningful — typical case is a full-graph "
            "  batch walk.\n"
            "* **List of clauses** (list[NodesScanOrderClause]): "
            "  lexicographic multi-key sort.  Each clause carries a "
            "  ``property`` name and an optional ``desc`` flag "
            "  (default ASC).  Mixed directions are supported; the "
            "  server expands the cursor-position predicate over the "
            "  full key sequence.  Note: the ``_node`` sentinel is "
            "  only valid as a single-key string form; use property "
            "  names in the list form."
        ),
    )
    limit: int = Field(
        default=DEFAULT_SCAN_LIMIT,
        ge=1,
        le=MAX_SCAN_LIMIT,
        description=(
            f"Max rows per response page.  Server may return fewer if "
            f"the payload cap is hit.  Hard cap {MAX_SCAN_LIMIT}."
        ),
    )
    cursor: str | None = Field(
        default=None,
        description=(
            "Opaque pagination token from a prior response's "
            "``next_cursor``.  Treat as a black box; round-trip "
            "unchanged."
        ),
    )

    @field_validator("labels")
    @classmethod
    def _validate_labels(cls, v: list[str]) -> list[str]:
        for label in v:
            if not _LABEL_PATTERN.fullmatch(label):
                raise ValueError(
                    f"label '{label}' must match {_LABEL_PATTERN.pattern}"
                )
        # De-dupe while preserving order so scan order is predictable.
        seen: set[str] = set()
        out: list[str] = []
        for label in v:
            if label not in seen:
                seen.add(label)
                out.append(label)
        return out

    @field_validator("project")
    @classmethod
    def _validate_project(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if v == ["*"]:
            return v
        for key in v:
            if key == "*":
                raise ValueError(
                    "'*' must appear alone in `project`; mixing it with "
                    "explicit names is ambiguous."
                )
            _validate_prop_key(key)
        return v

    @field_validator("order_by")
    @classmethod
    def _validate_order_by(
        cls, v: str | list[NodesScanOrderClause] | None,
    ) -> str | list[NodesScanOrderClause] | None:
        if v is None:
            # Explicit ``null`` → skip ORDER BY on the server.  Caller
            # opts out of pagination.  See model field docstring for
            # the AGE-planner rationale.
            return None
        if isinstance(v, str):
            # String form: either a property name or the _node sentinel.
            # Both pass _validate_prop_key (regex matches identifiers
            # starting with letter or underscore).
            return _validate_prop_key(v)
        # List form: each clause is already validated by NodesScanOrderClause.
        if not v:
            raise ValueError(
                "order_by list form must contain at least one clause"
            )
        # De-dupe by property name while preserving order — sorting on
        # the same property twice is meaningless and silently produces
        # an unstable plan in some engines.
        seen: set[str] = set()
        out: list[NodesScanOrderClause] = []
        for clause in v:
            if clause.property in seen:
                raise ValueError(
                    f"property '{clause.property}' appears more than once "
                    "in order_by — each key may appear at most once"
                )
            seen.add(clause.property)
            out.append(clause)
        return out


class NodesScanItem(BaseModel):
    """One node row in a /nodes:scan response.

    ``label`` is the resolved leaf label of the node: the label it was
    written under, which is a child label when the scan of a parent
    label reached it through ``label_hierarchy``.  Never the requested
    label for such a node — feeding the item back into ``nodes:bulk``
    writes it to its own label.
    """

    id: str = Field(..., description="Consumer-assigned external id.")
    label: str = Field(..., description="Resolved leaf label.")
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Projected properties.  Missing projected keys are absent "
            "(no nulls).  ``id`` is always present."
        ),
    )


class NodesScanResponse(BaseModel):
    """One page of a node scan."""

    tenant: str = Field(..., description="The tenant.")
    labels: list[str] = Field(..., description="The labels scanned, without repeats, in order.")
    items: list[NodesScanItem] = Field(
        default_factory=list,
        description="The nodes of this page, label by label, in `order_by` order.",
    )
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque cursor for the next page; ``null`` when the scan "
            "is fully drained."
        ),
    )
    scanned: int = Field(
        ...,
        description="Number of rows returned in this page.",
    )
    truncated_by_payload_cap: bool = Field(
        default=False,
        description=(
            "True when the page was cut short by the payload cap "
            "before ``limit`` was hit.  Caller pages normally via "
            "``next_cursor``."
        ),
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")


# ── Bulk edge enumeration (POST /edges:scan) ──────────────────────────────


class EdgesScanRequest(BaseModel):
    """Body for POST /v1/{tenant}/graph/edges:scan.

    Enumerate every edge whose ``type`` is in ``types`` (1+, OR-
    combined) AND that matches every ``where`` clause (AND-combined),
    with optional property projection and opaque cursor pagination.
    Same schema-agnostic shape as ``/nodes:scan``.

    Order is fixed in v1: by edge graphid PK ascending — always
    indexed, and edges don't have a consumer-assigned external id
    the way nodes do (they're keyed by ``(src_id, dst_id, type)``).
    Multi-key / property-based edge sort can be added later without
    breaking the v1 contract.

    Response items carry resolved endpoint info (``src_id`` /
    ``src_label`` / ``dst_id`` / ``dst_label``) so callers can
    round-trip directly to ``/edges:bulk`` — symmetric with the write
    shape.  The endpoint resolution does a JOIN per row and is
    bounded by ``limit``; on hot tenants the JOIN sits within ~100 ms.
    """

    types: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "1+ edge types to scan, OR-combined.  All must be "
            "registered on the tenant."
        ),
    )
    where: list[NodesScanWhere] = Field(
        default_factory=list,
        description=(
            "AND-combined filters on edge properties (same op set as "
            "``/nodes:scan``)."
        ),
    )
    project: list[str] | None = Field(
        default=None,
        description=(
            "Property names to return per edge.  Pass ``['*']`` for "
            "the full property bag.  Default ``None`` returns an "
            "empty property bag (callers that only want the topology "
            "+ type don't pay for property decode)."
        ),
    )
    limit: int = Field(
        default=DEFAULT_SCAN_LIMIT,
        ge=1,
        le=MAX_SCAN_LIMIT,
        description=(
            f"Max rows per response page.  Server may return fewer "
            f"if the payload cap is hit.  Hard cap {MAX_SCAN_LIMIT}."
        ),
    )
    cursor: str | None = Field(
        default=None,
        description=(
            "Opaque pagination token from a prior response's "
            "``next_cursor``.  Treat as a black box; round-trip "
            "unchanged."
        ),
    )

    @field_validator("types")
    @classmethod
    def _validate_types(cls, v: list[str]) -> list[str]:
        for t in v:
            if not _LABEL_PATTERN.fullmatch(t):
                raise ValueError(
                    f"edge type '{t}' must match {_LABEL_PATTERN.pattern}"
                )
        seen: set[str] = set()
        out: list[str] = []
        for t in v:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    @field_validator("project")
    @classmethod
    def _validate_project(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if v == ["*"]:
            return v
        for key in v:
            if key == "*":
                raise ValueError(
                    "'*' must appear alone in `project`; mixing it with "
                    "explicit names is ambiguous."
                )
            _validate_prop_key(key)
        return v


class EdgesScanItem(BaseModel):
    """One edge row in a /edges:scan response — shape mirrors EdgeUpsert
    so a caller can round-trip the row straight back through /edges:bulk
    if it wants to amend properties or migrate."""

    src_id: str = Field(..., description="Source node's external id.")
    src_label: str = Field(..., description="Source node's label.")
    dst_id: str = Field(..., description="Destination node's external id.")
    dst_label: str = Field(..., description="Destination node's label.")
    type: str = Field(..., description="Edge relationship type.")
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Projected edge properties.  Empty when ``project`` was "
            "omitted in the request."
        ),
    )


class EdgesScanResponse(BaseModel):
    """One page of an edge scan."""

    tenant: str = Field(..., description="The tenant.")
    types: list[str] = Field(..., description="The edge types scanned, without repeats, in order.")
    items: list[EdgesScanItem] = Field(
        default_factory=list,
        description="The edges of this page, type by type, in graph id order.",
    )
    next_cursor: str | None = Field(
        None,
        description=(
            "Opaque cursor for the next page (pass it as `cursor`); null when the scan is "
            "fully drained."
        ),
    )
    scanned: int = Field(..., description="Number of rows returned in this page.")
    truncated_by_payload_cap: bool = Field(
        False,
        description=(
            "True when the page was cut short by the 10 MiB payload cap before `limit` was hit."
        ),
    )
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")

"""Pydantic model round-trip tests.

Asserts the client's models accept the shapes the service emits.
"""

from __future__ import annotations

from graphos_client import (
    BulkNodesResponse,
    EdgeUpsert,
    ExpandResponse,
    NeighborsResponse,
    NodesUpsertResponse,
    NodeUpsert,
    PathHit,
    SearchResponse,
)


def test_node_upsert_minimal() -> None:
    n = NodeUpsert(id="x", label="Document")
    d = n.model_dump()
    assert d["id"] == "x"
    assert d["properties"] == {}


def test_node_upsert_round_trip() -> None:
    n = NodeUpsert(id="x", label="Document", properties={"name": "foo"})
    parsed = NodeUpsert.model_validate(n.model_dump())
    assert parsed.properties == {"name": "foo"}


def test_edge_upsert() -> None:
    e = EdgeUpsert(
        src_id="a", src_label="Document",
        dst_id="b", dst_label="Document", type="CITES",
        properties={"weight": 0.8},
    )
    assert e.type == "CITES"
    assert e.properties["weight"] == 0.8


def test_search_response_accepts_server_shape() -> None:
    payload = {
        "tenant": "demo",
        "query": "auth",
        "label": None,
        "limit": 10,
        "truncated": False,
        "elapsed_ms": 4,
        "nodes": [
            {"id": 12345, "label": "Document", "properties": {"name": "authorize"}},
        ],
    }
    parsed = SearchResponse.model_validate(payload)
    assert parsed.nodes[0].properties["name"] == "authorize"


def test_expand_response_accepts_paths() -> None:
    payload = {
        "tenant": "t", "node_id": "a", "max_hops": 2, "direction": "out",
        "rel_types": None, "limit": 100, "truncated": False, "elapsed_ms": 7,
        "paths": [
            {"end_id": "b", "end_label": "Document", "end_properties": {},
             "hops": 1, "rel_path": ["CITES"]},
        ],
    }
    parsed = ExpandResponse.model_validate(payload)
    assert isinstance(parsed.paths[0], PathHit)
    assert parsed.paths[0].rel_path == ["CITES"]


def test_neighbors_response_with_in_and_out() -> None:
    payload = {
        "tenant": "t", "node_id": "x", "limit": 50, "truncated": False,
        "elapsed_ms": 3,
        "neighbors": [
            {"neighbor_id": "a", "neighbor_label": "Document",
             "neighbor_properties": {}, "direction": "in",
             "edge_type": "CITES", "edge_properties": {}},
            {"neighbor_id": "b", "neighbor_label": "Document",
             "neighbor_properties": {}, "direction": "out",
             "edge_type": "CITES", "edge_properties": {"w": 0.9}},
        ],
    }
    parsed = NeighborsResponse.model_validate(payload)
    assert {n.direction for n in parsed.neighbors} == {"in", "out"}


def test_bulk_nodes_response_with_errors() -> None:
    payload = {
        "tenant": "t",
        "rows_received": 6,
        "rows_invalid": 3,
        "nodes_upserted": 3,
        "by_label": {"Document": 3},
        "errors": ["line 2: bad json", "line 4: missing field"],
        "elapsed_ms": 10,
    }
    parsed = BulkNodesResponse.model_validate(payload)
    assert parsed.rows_invalid == 3
    assert len(parsed.errors) == 2


def test_nodes_upsert_response_default_counts() -> None:
    parsed = NodesUpsertResponse.model_validate(
        {"tenant": "t", "nodes_upserted": 0, "elapsed_ms": 1},
    )
    assert parsed.by_label == {}

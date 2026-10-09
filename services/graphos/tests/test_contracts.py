"""Contract validation tests — wire-shape rules for tenant registration."""

from __future__ import annotations

import pytest

from graphos.contracts.graph import (
    MAX_EDGES_UPSERT_BATCH,
    MAX_NODES_UPSERT_BATCH,
    EdgesUpsertRequest,
    EdgeUpsert,
    NodesUpsertRequest,
    NodeUpsert,
)
from graphos.contracts.louvain import RecomputeCommunitiesRequest
from graphos.contracts.tenants import TenantSpecBody
from graphos.louvain import _build_node_filter_sql


def test_valid_spec() -> None:
    body = TenantSpecBody(
        name="demo",
        vertex_labels=["Document", "Collection", "Team"],
        edge_labels=["CITES", "REFERENCES", "TAGGED_WITH"],
        property_indexes=[{"label": "Document", "properties": ["name"]}],
        generated_columns=[],
        meta={"owner": "demo"},
    )
    assert body.name == "demo"
    assert "Document" in body.vertex_labels


@pytest.mark.parametrize(
    "bad_name",
    [
        "demo!",       # special char
        "1demo",       # digit start
        "",                # empty
        "a" * 64,          # too long
        "consumer-x",      # hyphen not allowed
        "consumer space",  # whitespace not allowed
    ],
)
def test_rejects_bad_tenant_name(bad_name: str) -> None:
    with pytest.raises(ValueError):
        TenantSpecBody(name=bad_name)


@pytest.mark.parametrize(
    "good_name",
    [
        "demo",
        "TEST_TENANT",         # uppercase is fine — AGE preserves case
        "Acme_v2",
        "a",
    ],
)
def test_accepts_mixed_case_tenant_name(good_name: str) -> None:
    body = TenantSpecBody(name=good_name)
    assert body.name == good_name


@pytest.mark.parametrize(
    "bad_label",
    [
        "document",         # lowercase start
        "Document!",        # special char
        "1Document",        # digit start
    ],
)
def test_rejects_bad_label_name(bad_label: str) -> None:
    with pytest.raises(ValueError):
        TenantSpecBody(name="t", vertex_labels=[bad_label])


def test_empty_spec_is_valid() -> None:
    """A tenant with just a name (no labels yet) must be allowed —
    registering an empty graph is meaningful."""
    body = TenantSpecBody(name="empty_tenant")
    assert body.vertex_labels == []
    assert body.edge_labels == []


def test_spec_accepts_label_hierarchy() -> None:
    body = TenantSpecBody(
        name="demo",
        vertex_labels=["Document", "Article", "Section"],
        edge_labels=["CITES"],
        label_hierarchy={"Article": "Document", "Section": "Document"},
    )
    assert body.label_hierarchy == {"Article": "Document", "Section": "Document"}


def test_spec_rejects_self_loop_hierarchy() -> None:
    with pytest.raises(ValueError, match="self-loops"):
        TenantSpecBody(
            name="t",
            vertex_labels=["Document"],
            label_hierarchy={"Document": "Document"},
        )


@pytest.mark.parametrize("bad", ["document", "Document!", "1Document"])
def test_spec_rejects_bad_hierarchy_label(bad: str) -> None:
    with pytest.raises(ValueError):
        TenantSpecBody(
            name="t",
            vertex_labels=["Document"],
            label_hierarchy={bad: "Document"},
        )


# ── Upsert contracts ──────────────────────────────────────────────────────


def test_valid_upsert_batch() -> None:
    body = NodesUpsertRequest(
        nodes=[
            NodeUpsert(id="demo/svc/foo.py:Bar.baz", label="Document",
                       properties={"project_id": "demo", "name": "baz"}),
            NodeUpsert(id="cmp-001", label="Collection",
                       properties={"name": "auth-svc"}),
        ],
    )
    assert len(body.nodes) == 2
    assert body.nodes[0].label == "Document"


def test_empty_batch_rejected() -> None:
    with pytest.raises(ValueError):
        NodesUpsertRequest(nodes=[])


def test_batch_too_large_rejected() -> None:
    too_many = [
        NodeUpsert(id=f"id-{i}", label="Document")
        for i in range(MAX_NODES_UPSERT_BATCH + 1)
    ]
    with pytest.raises(ValueError):
        NodesUpsertRequest(nodes=too_many)


@pytest.mark.parametrize("bad_label", ["document", "Document!", "1Document"])
def test_node_upsert_rejects_bad_label(bad_label: str) -> None:
    with pytest.raises(ValueError):
        NodeUpsert(id="x", label=bad_label)


def test_node_upsert_rejects_empty_id() -> None:
    with pytest.raises(ValueError):
        NodeUpsert(id="", label="Document")


# ── Edge upsert ─────────────────────────────────────────────────────────────


def test_valid_edge_batch() -> None:
    body = EdgesUpsertRequest(
        edges=[
            EdgeUpsert(
                src_id="a", src_label="Document",
                dst_id="b", dst_label="Document",
                type="CITES",
                properties={"weight": 0.8},
            ),
            EdgeUpsert(
                src_id="x", src_label="Document",
                dst_id="y", dst_label="Collection",
                type="PART_OF",
            ),
        ],
    )
    assert len(body.edges) == 2
    assert body.edges[0].type == "CITES"


def test_empty_edge_batch_rejected() -> None:
    with pytest.raises(ValueError):
        EdgesUpsertRequest(edges=[])


def test_edge_batch_too_large_rejected() -> None:
    too_many = [
        EdgeUpsert(src_id=f"a{i}", src_label="Document",
                   dst_id=f"b{i}", dst_label="Document", type="CITES")
        for i in range(MAX_EDGES_UPSERT_BATCH + 1)
    ]
    with pytest.raises(ValueError):
        EdgesUpsertRequest(edges=too_many)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("src_label", "document"),
        ("dst_label", "Document!"),
        ("type",       "1CALLS"),
    ],
)
def test_edge_upsert_rejects_bad_label_or_type(field: str, bad: str) -> None:
    base = {
        "src_id": "a", "src_label": "Document",
        "dst_id": "b", "dst_label": "Document",
        "type": "CITES",
    }
    base[field] = bad
    with pytest.raises(ValueError):
        EdgeUpsert(**base)


def test_edge_upsert_rejects_empty_ids() -> None:
    with pytest.raises(ValueError):
        EdgeUpsert(src_id="", src_label="Document",
                   dst_id="b", dst_label="Document", type="CITES")
    with pytest.raises(ValueError):
        EdgeUpsert(src_id="a", src_label="Document",
                   dst_id="", dst_label="Document", type="CITES")


# ── Louvain node_filter ────────────────────────────────────────────────────


def test_recompute_request_no_filter() -> None:
    body = RecomputeCommunitiesRequest()
    assert body.node_filter is None


def test_recompute_request_with_string_filter() -> None:
    body = RecomputeCommunitiesRequest(
        node_filter={"project_id": "demo"},
    )
    assert body.node_filter == {"project_id": "demo"}


def test_recompute_request_with_list_filter() -> None:
    body = RecomputeCommunitiesRequest(
        node_filter={"project_id": ["demo", "bridge"]},
    )
    assert body.node_filter == {"project_id": ["demo", "bridge"]}


@pytest.mark.parametrize("bad_key", ["bad-key", "1key", "key!", "drop table"])
def test_recompute_request_rejects_bad_filter_key(bad_key: str) -> None:
    with pytest.raises(ValueError):
        RecomputeCommunitiesRequest(node_filter={bad_key: "x"})


def test_build_node_filter_sql_scalar() -> None:
    frags, params = _build_node_filter_sql({"project_id": "demo"})
    assert frags == [
        "src.properties->'\"project_id\"' IN (to_jsonb($1::text)::text::agtype)",
        "dst.properties->'\"project_id\"' IN (to_jsonb($2::text)::text::agtype)",
    ]
    assert params == ["demo", "demo"]


def test_build_node_filter_sql_list() -> None:
    frags, params = _build_node_filter_sql({"project_id": ["demo", "bridge"]}, "a", "b")
    assert frags == [
        "a.properties->'\"project_id\"' IN "
        "(to_jsonb($1::text)::text::agtype, to_jsonb($2::text)::text::agtype)",
        "b.properties->'\"project_id\"' IN "
        "(to_jsonb($3::text)::text::agtype, to_jsonb($4::text)::text::agtype)",
    ]
    assert params == ["demo", "bridge", "demo", "bridge"]


def test_build_node_filter_sql_empty() -> None:
    assert _build_node_filter_sql(None) == ([], [])
    assert _build_node_filter_sql({}) == ([], [])

"""A tenant's schema manager and the drift report (graphos.schema_drift, ADR-0013): no DB."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from graphos import schema_changes, schema_drift
from graphos.bootstrap import TenantSpec

STORED = TenantSpec(
    name="acme",
    vertex_labels=("Doc", "Team", "Note"),
    edge_labels=("OWNS", "LINKS_TO"),
    edge_endpoints=(("OWNS", "Team", "Doc"),),
    meta={"backend": "loader", "owner": "ops"},
    search_indexes={"titles": {"labels": ["Doc"], "properties": ["title"]}},
    display={"vertex": {"Doc": {"color": "#336699"}, "Team": {"color": "#993366"}}},
    property_schema={"vertex": {"Doc": {"properties": {"title": {"type": "string"}}}}},
    deprecated={"vertex": {"Note": {"deprecated": True}}},
)


def test_only_styles_are_presentation() -> None:
    assert not schema_drift.changes_structure(None)
    assert not schema_drift.changes_structure({"changed": {"display": ["vertex:Doc"]}})
    assert not schema_drift.changes_structure({"added": {"display": ["vertex:Note"]}})
    assert schema_drift.changes_structure({"added": {"vertex_labels": ["X"]}})
    assert schema_drift.changes_structure({"changed": {"meta": ["owner"]}})
    assert schema_drift.changes_structure({"changed": {"property_schema": ["vertex:Doc"]}})
    assert schema_drift.changes_structure({"strict_endpoints": [False, True]})
    assert schema_drift.changes_structure(
        {"added": {"display": ["vertex:Note"], "edge_labels": ["CITES"]}})


def test_a_change_is_judged_on_what_it_changes() -> None:
    # Declaring again what is stored changes nothing: never refused.
    assert not schema_drift.changes_structure(schema_changes.diff(STORED, STORED))
    restyled = replace(STORED, display={"vertex": {"Doc": {"color": "#000000"}}})
    assert not schema_drift.changes_structure(schema_changes.diff(STORED, restyled))
    relabelled = replace(STORED, vertex_labels=(*STORED.vertex_labels, "Extra"))
    assert schema_drift.changes_structure(schema_changes.diff(STORED, relabelled))


def test_only_the_manager_may_change_a_managed_tenant() -> None:
    schema_drift.check("acme", None, "user:olive")
    schema_drift.check("acme", "user:loader", "user:loader")
    with pytest.raises(schema_drift.SchemaManaged) as refused:
        schema_drift.check("acme", "user:loader", "user:olive")
    assert refused.value.manager == "user:loader"
    assert "managed by user:loader" in str(refused.value)
    with pytest.raises(schema_drift.SchemaManaged):
        schema_drift.check("acme", "user:loader", None)   # the anonymous admin too


def test_the_managers_registration_keeps_peoples_styles() -> None:
    declared = TenantSpec(name="acme", display={
        "vertex": {"Doc": {"color": "#000000"}, "Note": {"color": "#00AA00"}},
        "edge": {"OWNS": {"color": "#AA0000"}},
    })
    kept, removed = schema_drift.keep_peoples_styles(
        STORED, declared, (("vertex", "Team"),))
    assert kept.display == {"vertex": {"Note": {"color": "#00AA00"}},
                            "edge": {"OWNS": {"color": "#AA0000"}}}
    assert removed == ()   # nor does it remove one


def test_the_drift_report_says_what_differs() -> None:
    declared = TenantSpec(
        name="acme",
        vertex_labels=("Doc", "Team", "Old"),
        edge_labels=("OWNS",),
        meta={"backend": "loader", "owner": "dev"},
        search_indexes={"titles": {"labels": ["Doc"], "properties": ["name"]}},
        display={"vertex": {"Doc": {"color": "#000000"}}},
    )
    changes = {"changed": {"meta": ["owner"], "display": ["vertex:Doc"]}}
    removed_at = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    removals = [
        {"kind": "vertex_label", "name": "Old", "src_label": "", "dst_label": "",
         "removed_at": removed_at, "removed_by": "olive"},
        {"kind": "edge_type", "name": "GONE", "src_label": "", "dst_label": "",
         "removed_at": removed_at, "removed_by": "olive"},
    ]
    report = schema_drift.drift(STORED, declared, changes, removals)
    assert report["not_declared"] == {
        "vertex_labels": ["Note"],
        "edge_labels": ["LINKS_TO"],
        "edge_endpoints": [["OWNS", "Team", "Doc"]],
        "display": {"vertex": ["Team"]},
        "property_schema": {"vertex": ["Doc"]},
        "deprecated": {"vertex": ["Note"]},
    }
    assert report["overwrites"] == {
        "meta": ["owner"], "display": {"vertex": ["Doc"]}, "search_indexes": ["titles"],
    }
    # Only what the body declares again; "GONE" is not declared.
    assert report["recreates"] == [{
        "kind": "vertex_label", "name": "Old", "src_label": None, "dst_label": None,
        "removed_at": removed_at.isoformat(), "removed_by": "olive",
    }]


def test_a_removed_pair_declared_again_is_reported() -> None:
    declared = TenantSpec(name="acme", edge_endpoints=(("OWNS", "Team", "Note"),))
    removals = [{"kind": "edge_endpoint", "name": "OWNS", "src_label": "Team",
                 "dst_label": "Note", "removed_at": None, "removed_by": None}]
    report = schema_drift.drift(STORED, declared, None, removals)
    assert report["recreates"][0]["src_label"] == "Team"


def test_nothing_differs_nothing_reported() -> None:
    assert schema_drift.drift(STORED, STORED, None, []) == {}

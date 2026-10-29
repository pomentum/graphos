"""A tenant's schema manager and the drift report (graphos.schema_drift,
ADR-0013, ADR-0014): no DB."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from graphos import schema_changes, schema_drift
from graphos.bootstrap import TenantSpec
from graphos.identity import AccessStore, Principal
from graphos.oidc import Identity

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
    community_algorithm="louvain",
)
LOADER = Principal.static("loader")
OLIVE = Principal.static("olive")


def test_only_styles_are_presentation() -> None:
    assert not schema_drift.changes_structure(None)
    assert not schema_drift.changes_structure({"changed": {"display": ["vertex:Doc"]}})
    assert not schema_drift.changes_structure({"added": {"display": ["vertex:Note"]}})
    assert not schema_drift.changes_structure({"schema_manager": [None, "user:loader"]})
    assert schema_drift.changes_structure({"added": {"vertex_labels": ["X"]}})
    assert schema_drift.changes_structure({"changed": {"meta": ["owner"]}})
    assert schema_drift.changes_structure({"changed": {"search_indexes": ["titles"]}})
    assert schema_drift.changes_structure({"strict_endpoints": [False, True]})


def test_a_change_is_judged_on_what_it_changes() -> None:
    # Declaring again what is stored changes nothing: never refused.
    assert not schema_drift.changes_structure(schema_changes.diff(STORED, STORED))
    restyled = replace(STORED, display={"vertex": {"Doc": {"color": "#000000"}}})
    assert not schema_drift.changes_structure(schema_changes.diff(STORED, restyled))
    relabelled = replace(STORED, vertex_labels=(*STORED.vertex_labels, "Extra"))
    assert schema_drift.changes_structure(schema_changes.diff(STORED, relabelled))


def test_a_search_index_redefined_under_its_name_is_a_change() -> None:
    redefined = replace(STORED, search_indexes={
        "titles": {"labels": ["Doc"], "properties": ["name"]}})
    detail = schema_changes.diff(STORED, redefined)
    assert detail == {"changed": {"search_indexes": ["titles"]}}
    assert schema_drift.changes_structure(detail)


def test_the_community_algorithm_is_not_structure() -> None:
    leiden = replace(STORED, community_algorithm="leiden")
    assert not schema_drift.changes_structure(schema_changes.diff(STORED, leiden))


def test_callers_are_named_as_managers() -> None:
    assert schema_drift.principal_subject(LOADER) == "user:loader"
    # Built as a sign-in through the identity provider builds it (no pool needed).
    store = AccessStore(cast(Any, None))
    sso = store.principal_for(Identity(subject="0a1b-sub", consumer="Ada"))
    assert schema_drift.principal_subject(sso) == "oidc-sub:0a1b-sub"
    assert schema_drift.principal_subject(Principal(consumer="x", subject="raw")) is None
    assert schema_drift.principal_subject(Principal(consumer="anonymous", admin=True)) is None


def test_only_the_manager_may_change_a_managed_tenant() -> None:
    schema_drift.check("acme", None, OLIVE)
    schema_drift.check("acme", "user:loader", LOADER)
    with pytest.raises(schema_drift.SchemaManaged) as refused:
        schema_drift.check("acme", "user:loader", OLIVE)
    assert refused.value.manager == "user:loader"
    assert "managed by user:loader" in str(refused.value)
    with pytest.raises(schema_drift.SchemaManaged):
        schema_drift.check("acme", "user:loader", Principal(consumer="anonymous", admin=True))


def test_missing_only_adds_new_styles_and_keeps_the_stored_ones() -> None:
    declared = replace(STORED, display={
        "vertex": {"Doc": {"color": "#000000"}, "Note": {"color": "#00AA00"}},
        "edge": {"OWNS": {"color": "#AA0000"}},
    })
    merged, removed = schema_drift.apply_display_merge(
        STORED, declared, (("vertex", "Team"),), "missing_only", replace_all=False)
    assert merged.display == {"vertex": {"Note": {"color": "#00AA00"}},
                              "edge": {"OWNS": {"color": "#AA0000"}}}
    assert removed == ()


def test_a_replace_under_missing_only_keeps_every_stored_style() -> None:
    declared = replace(STORED, display={"vertex": {"Note": {"color": "#00AA00"}}})
    merged, _ = schema_drift.apply_display_merge(
        STORED, declared, (), "missing_only", replace_all=True)
    assert merged.display["vertex"] == {
        "Doc": {"color": "#336699"}, "Team": {"color": "#993366"}, "Note": {"color": "#00AA00"},
    }


def test_a_replace_drops_the_styles_of_the_labels_it_removes() -> None:
    declared = replace(STORED, vertex_labels=("Doc", "Note"),
                       edge_endpoints=(), display={})
    merged, _ = schema_drift.apply_display_merge(
        STORED, declared, (), "missing_only", replace_all=True)
    assert merged.display == {"vertex": {"Doc": {"color": "#336699"}}}


def test_entries_merges_as_every_registration_does() -> None:
    declared = replace(STORED, display={"vertex": {"Doc": {"color": "#000000"}}})
    merged, removed = schema_drift.apply_display_merge(
        STORED, declared, (("vertex", "Team"),), "entries", replace_all=False)
    assert merged is declared and removed == (("vertex", "Team"),)


def _removal(kind: str, name: str, src: str = "", dst: str = "") -> dict[str, object]:
    return {"kind": kind, "name": name, "src_label": src, "dst_label": dst,
            "removed_at": datetime(2026, 10, 29, 12, 0, tzinfo=UTC), "removed_by": "olive"}


def test_the_drift_report_of_a_merge() -> None:
    declared = TenantSpec(
        name="acme",
        vertex_labels=("Doc", "Team", "Old"),
        edge_labels=("OWNS",),
        meta={"backend": "loader", "owner": "dev"},
        search_indexes={"titles": {"labels": ["Doc"], "properties": ["name"]}},
        display={"vertex": {"Doc": {"color": "#000000"}}},
        community_algorithm="leiden",
        strict_endpoints=True,
    )
    after = replace(STORED, meta={**STORED.meta, "owner": "dev"},
                    search_indexes=declared.search_indexes,
                    display={"vertex": {**STORED.display["vertex"], **declared.display["vertex"]}},
                    vertex_labels=(*STORED.vertex_labels, "Old"), strict_endpoints=True)
    report = schema_drift.drift(STORED, declared, after, replace_all=False, removals=[
        _removal("vertex_label", "Old"), _removal("edge_type", "GONE"),
    ])
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
    assert report["settings"] == {"strict_endpoints": [False, True],
                                  "community_algorithm": ["louvain", "leiden"]}
    # Only what the body declares again; "GONE" is not declared.
    assert report["recreates"] == {"vertex_labels": [{
        "name": "Old", "removed_at": "2026-10-29T12:00:00+00:00", "removed_by": "olive"}]}
    assert "removes" not in report


def test_a_replace_reports_what_it_removes_not_what_is_not_declared() -> None:
    declared = TenantSpec(name="acme", vertex_labels=("Doc", "Team"), edge_labels=("OWNS",))
    after = replace(STORED, vertex_labels=("Doc", "Team"), edge_labels=("OWNS",),
                    edge_endpoints=(), deprecated={})
    report = schema_drift.drift(STORED, declared, after, replace_all=True, removals=[])
    assert "not_declared" not in report
    assert report["removes"]["vertex_labels"] == ["Note"]
    assert report["removes"]["edge_labels"] == ["LINKS_TO"]
    assert report["removes"]["edge_endpoints"] == [["OWNS", "Team", "Doc"]]


def test_the_managers_own_styles_are_not_reported_as_not_declared() -> None:
    # The report is about what was declared, before the styles merge.
    report = schema_drift.drift(STORED, STORED, STORED, replace_all=False, removals=[])
    assert report == {}


def test_a_removed_pair_declared_again_is_reported() -> None:
    declared = TenantSpec(name="acme", edge_endpoints=(("OWNS", "Team", "Note"),))
    report = schema_drift.drift(STORED, declared, STORED, replace_all=False, removals=[
        _removal("edge_endpoint", "OWNS", "Team", "Note")])
    assert report["recreates"]["edge_endpoints"][0]["pair"] == ["OWNS", "Team", "Note"]


def test_a_name_the_tenant_has_again_is_not_recreated() -> None:
    # "Note" was dropped, then a rename gave the name back: declaring it is no recreation.
    declared = TenantSpec(name="acme", vertex_labels=("Note",))
    report = schema_drift.drift(STORED, declared, STORED, replace_all=False, removals=[
        _removal("vertex_label", "Note")])
    assert "recreates" not in report

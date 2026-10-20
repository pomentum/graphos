"""Deprecated and renamed names (ADR-0009), without a database: the checks a write
meets, what a registration keeps, and what the answers say."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from graphos import properties, renames, schema_changes, schema_document, schema_rename
from graphos.bootstrap import TenantSpec, spec_from_json, spec_to_json

NOW = datetime(2026, 10, 19, 9, 30, tzinfo=UTC)
SPEC = TenantSpec(
    name="acme",
    vertex_labels=("Person", "Employee", "Servce", "Service"),
    edge_labels=("KNOWS", "DEPENDS", "DEPENDS_ON"),
    label_hierarchy=(("Employee", "Person"),),
    deprecated={
        "vertex": {
            "Servce": {"deprecated": True, "replaced_by": "Service", "note": "a typo"},
            "Person": {"properties": {"nick": {"replaced_by": "alias"}}},
        },
        "edge": {"DEPENDS": {"deprecated": True, "replaced_by": "DEPENDS_ON"}},
    },
    renames=(
        {"kind": "label", "from": "Team", "to": "Group", "at": "2026-10-18T10:00:00+00:00"},
        {"kind": "property", "from": "mail", "to": "email", "on": "Person",
         "at": "2026-10-18T11:00:00+00:00", "status": "done"},
        {"kind": "property", "from": "tel", "to": "phone", "on": "Person",
         "at": "2026-10-19T08:00:00+00:00", "status": "running"},
    ),
)


def _policies(mode: str | None = None) -> properties.Policies:
    spec = SPEC if mode is None else replace(SPEC, property_schema={"mode": mode})
    return properties.policies_for(spec)


def _rules(policy: properties.Policy, row: dict[str, object]) -> list[tuple[str, str, str]]:
    return [(v.property, v.rule, v.severity) for v in policy.check(row)]


# ── The checks a write meets ───────────────────────────────────────────────


@pytest.mark.parametrize("mode", [None, "warn", "strict"])
def test_a_deprecated_label_or_property_warns_and_never_refuses(mode: str | None) -> None:
    policies = _policies(mode)
    tally = properties.enforce([
        (policies.for_vertex("Servce"), {"name": "a"}, "s1", {}),
        (policies.for_vertex("Person"), {"nick": "Al"}, "p1", {}),
    ])
    assert tally.violations == {}
    assert tally.warnings == {"Servce.*:deprecated": 1, "Person.nick:deprecated": 1}
    assert "use 'Service': a typo" in policies.for_vertex("Servce").deprecated_label


def test_a_deprecated_type_warns_on_every_edge() -> None:
    policy = _policies().for_edge("DEPENDS")
    assert _rules(policy, {}) == [("*", "deprecated", "warning")]
    assert _rules(_policies().for_edge("DEPENDS_ON"), {}) == []


def test_a_property_renamed_away_is_refused_in_every_mode_once_done() -> None:
    for mode in (None, "warn", "strict"):
        with pytest.raises(properties.Refused) as refused:
            properties.enforce([(_policies(mode).for_vertex("Person"), {"mail": "a@b"}, "p1", {})])
        assert refused.value.listed[0]["message"] == "renamed to 'email'"


def test_a_property_being_renamed_only_warns() -> None:
    policy = _policies().for_vertex("Person")
    assert _rules(policy, {"tel": "1"}) == [("tel", "renamed", "warning")]
    tally = properties.enforce([(policy, {"tel": "1"}, "p1", {})])
    assert tally.warnings == {"Person.tel:renamed": 1}


def test_a_child_label_inherits_its_parents_deprecations_and_renames() -> None:
    policy = _policies().for_vertex("Employee")
    assert {v.rule for v in policy.check({"nick": "x", "tel": "1"})} == {"deprecated", "renamed"}
    assert policy.deprecated_label is None


def test_without_deprecations_nothing_is_checked_in_off_mode() -> None:
    plain = properties.policies_for(TenantSpec(name="t", vertex_labels=("A",)))
    assert not plain.any_checks
    assert plain.for_vertex("A").check({"x": 1}) == []
    assert _policies().any_checks


# ── What a registration keeps ──────────────────────────────────────────────


def test_a_spec_round_trips_its_deprecations_and_renames() -> None:
    back = spec_from_json(spec_to_json(SPEC))
    assert back.deprecated == SPEC.deprecated
    assert back.renames == SPEC.renames


def test_a_merge_replaces_deprecations_by_entry_and_keeps_the_renames() -> None:
    incoming = TenantSpec(
        name="acme",
        deprecated={"vertex": {"Person": {"properties": {"age": {}}}}},
        renames=({"kind": "label", "from": "X", "to": "Y"},),
    )
    after = schema_document.merged(
        SPEC, incoming, replace=False, endpoints_stated=False,
        deprecated_removed=(("edge", "DEPENDS"),),
    )
    assert after.deprecated == {
        "vertex": {
            "Servce": SPEC.deprecated["vertex"]["Servce"],
            "Person": {"properties": {"age": {}}},
        },
    }
    assert after.renames == SPEC.renames
    replaced = schema_document.merged(SPEC, incoming, replace=True, endpoints_stated=False)
    assert replaced.renames == SPEC.renames


def test_the_export_carries_deprecations_as_version_3() -> None:
    doc = schema_document.document(SPEC, now=NOW)
    assert doc["_export"]["version"] == 3  # noqa: PLR2004
    assert doc["deprecated"]["edge"] == {"DEPENDS": SPEC.deprecated["edge"]["DEPENDS"]}
    assert "renames" not in doc
    assert "deprecated" not in schema_document.document(replace(SPEC, deprecated={}), now=NOW)


# ── What the answers say ───────────────────────────────────────────────────


def test_a_name_renamed_away_answers_where_it_went() -> None:
    assert renames.why_unknown(SPEC, "Team") == (
        "'Team' was renamed to 'Group' on 2026-10-18: use the new name"
    )
    assert renames.why_unknown(SPEC, "Nope") is None
    with pytest.raises(HTTPException) as refused:
        renames.refuse_renamed(SPEC, "Team")
    assert refused.value.status_code == 409  # noqa: PLR2004
    renames.refuse_renamed(SPEC, None)


def test_a_name_registered_again_since_is_not_renamed_away() -> None:
    again = replace(SPEC, vertex_labels=(*SPEC.vertex_labels, "Team"))
    assert renames.renamed_away(again, "Team") is None


def test_a_registration_that_deprecates_is_recorded_in_the_feed() -> None:
    before = replace(SPEC, deprecated={})
    detail = schema_changes.diff(before, SPEC)
    assert detail is not None
    assert detail["added"]["deprecated"] == ["edge:DEPENDS", "vertex:Person", "vertex:Servce"]


# ── A label or type renamed in the spec (graphos.schema_rename) ────────────


def test_a_renamed_label_is_renamed_everywhere_the_spec_names_it() -> None:
    spec = replace(
        SPEC,
        property_indexes=({"label": "Service", "properties": ["kind"]},),
        search_indexes={"s": {"labels": ["Service"], "properties": ["name"]}},
        resolve_rules=(("DEPENDS_ON", {"require_dst_label": ["Service", "Person"]}),),
        edge_endpoints=(("DEPENDS_ON", "Service", "Service"),),
        display={"vertex": {"Service": {"color": "#112233"}}},
    )
    entry = {"kind": "label", "from": "Service", "to": "System"}
    after = schema_rename.renamed_spec(spec, "label", "Service", "System", entry)
    assert "System" in after.vertex_labels and "Service" not in after.vertex_labels
    assert after.property_indexes[0]["label"] == "System"
    assert after.search_indexes["s"]["labels"] == ["System"]
    assert after.resolve_rules[0][1]["require_dst_label"] == ["System", "Person"]
    assert after.edge_endpoints == (("DEPENDS_ON", "System", "System"),)
    assert "System" in after.display["vertex"]
    assert after.deprecated["vertex"]["Servce"]["replaced_by"] == "System"
    assert after.renames[-1] == entry
    assert set(schema_rename.spec_parts_named(spec, "label", "Service")) == {
        "property_indexes", "search_indexes", "resolve_rules", "display", "deprecated",
    }


def test_a_renamed_edge_type_takes_its_rules_and_pairs() -> None:
    spec = replace(SPEC, resolve_rules=(("DEPENDS", {"src_has_array": "x"}),),
                   edge_endpoints=(("DEPENDS", "Service", "Service"),))
    after = schema_rename.renamed_spec(spec, "edge_type", "DEPENDS", "USES", {})
    assert after.edge_labels == ("KNOWS", "USES", "DEPENDS_ON")
    assert after.resolve_rules[0][0] == "USES"
    assert after.edge_endpoints == (("USES", "Service", "Service"),)
    assert "USES" in after.deprecated["edge"]


@pytest.mark.parametrize(("query", "named"), [
    ("MATCH (d:Docu) RETURN d", True),
    ("MATCH (:Docu)-[:WROTE]->(x) RETURN x", True),
    ("MATCH (d: `Docu`) RETURN d", True),
    ("MATCH (d:Document) RETURN d", False),
    ("MATCH (d:Doc) RETURN d.Docu", False),
])
def test_a_saved_query_names_a_label(query: str, named: bool) -> None:  # noqa: FBT001
    assert schema_rename.mentions(query, "Docu") is named

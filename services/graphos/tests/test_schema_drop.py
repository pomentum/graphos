"""Dropping a deprecated label or edge type (ADR-0009 §1): what is refused,
and what the spec looks like without it."""

from __future__ import annotations

import pytest

from graphos.bootstrap import TenantSpec
from graphos.schema_rename import RenameError, check_drop, dropped_spec

SPEC = TenantSpec(
    name="t",
    vertex_labels=("Person", "Legacy", "Base", "Child", "Fresh"),
    edge_labels=("KNOWS", "LINKS"),
    label_hierarchy=(("Child", "Base"),),
    property_indexes=({"label": "Legacy", "properties": ["kind"]},),
    generated_columns=({"label": "Legacy", "property": "rank", "type": "int"},),
    search_indexes={
        "legacy_only": {"labels": ["Legacy"], "properties": ["title"]},
        "both": {"labels": ["Person", "Legacy"], "properties": ["name"]},
    },
    resolve_rules=(
        ("LINKS", {"src_has_array": "tags", "dst_value_from": "name"}),
        ("KNOWS", {"src_has_array": "friends", "dst_value_from": "name",
                   "require_dst_label": ["Person"]}),
    ),
    edge_endpoints=(("KNOWS", "Person", "Legacy"), ("LINKS", "Person", "Person")),
    display={"vertex": {"Legacy": {"color": "#111111"}, "Person": {"color": "#222222"}},
             "edge": {"LINKS": {"color": "#333333"}}},
    property_schema={"mode": "warn", "vertex": {"Legacy": {"properties": {}}},
                     "edge": {"LINKS": {"properties": {}}}},
    deprecated={
        "vertex": {"Legacy": {"deprecated": True, "replaced_by": "Person"},
                   "Base": {"deprecated": True},
                   "Fresh": {"deprecated": True, "replaced_by": "Legacy"}},
        "edge": {"LINKS": {"deprecated": True}},
    },
)


@pytest.mark.parametrize(("name", "why"), [
    ("Nope", "is not a vertex label or edge type"),
    ("Person", "is not deprecated"),
    ("KNOWS", "is not deprecated"),
    ("Base", "Child inherit from 'Base'"),
    ("Legacy", "Fresh name 'Legacy' as their replacement"),
])
def test_what_a_drop_refuses(name: str, why: str) -> None:
    with pytest.raises(RenameError, match=why):
        check_drop(SPEC, name)


def test_a_resolve_rule_requiring_the_label_refuses_its_drop() -> None:
    spec = TenantSpec(
        name="t", vertex_labels=("Person",), edge_labels=("KNOWS",),
        resolve_rules=(("KNOWS", {"src_has_array": "f", "dst_value_from": "n",
                                  "require_dst_label": ["Person"]}),),
        deprecated={"vertex": {"Person": {"deprecated": True}}},
    )
    with pytest.raises(RenameError, match="resolve rules of KNOWS require 'Person'"):
        check_drop(spec, "Person")


def test_a_vertex_label_leaves_everything_that_named_it() -> None:
    after = dropped_spec(SPEC, check_drop(SPEC, "Fresh"), "Fresh")
    assert "Fresh" not in after.vertex_labels
    assert "Fresh" not in after.deprecated["vertex"]
    after = dropped_spec(after, check_drop(after, "Legacy"), "Legacy")
    assert after.vertex_labels == ("Person", "Base", "Child")
    assert after.property_indexes == () and after.generated_columns == ()  # noqa: PT018
    assert after.search_indexes == {"both": {"labels": ["Person"], "properties": ["name"]}}
    assert after.edge_endpoints == (("LINKS", "Person", "Person"),)
    assert after.display["vertex"] == {"Person": {"color": "#222222"}}
    assert after.property_schema == {"mode": "warn", "edge": {"LINKS": {"properties": {}}}}
    assert set(after.deprecated["vertex"]) == {"Base"}


def test_an_edge_type_leaves_its_rule_pairs_and_styles() -> None:
    after = dropped_spec(SPEC, check_drop(SPEC, "LINKS"), "LINKS")
    assert after.edge_labels == ("KNOWS",)
    assert [t for t, _ in after.resolve_rules] == ["KNOWS"]
    assert after.edge_endpoints == (("KNOWS", "Person", "Legacy"),)
    assert "edge" not in after.display and "edge" not in after.deprecated  # noqa: PT018
    assert after.property_schema == {"mode": "warn", "vertex": {"Legacy": {"properties": {}}}}

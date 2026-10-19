"""Which re-registrations a dry run reports as breaking (``graphos.schema_breaking``)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from graphos.bootstrap import TenantSpec
from graphos.schema_breaking import changes

BASE = TenantSpec(
    name="acme", vertex_labels=("Doc", "Tag", "Report"), edge_labels=("LINKS_TO", "TAGGED"),
    label_hierarchy=(("Report", "Doc"),),
    search_indexes={"names": {"labels": ["Doc"], "properties": ["name"], "analyzer": "text"}},
    edge_endpoints=(("TAGGED", "Doc", "Tag"),),
)


def _schema(mode: str, **doc: Any) -> dict[str, Any]:
    return {"mode": mode, "vertex": {"Doc": {"properties": doc}}}


def _codes(before: TenantSpec, after: TenantSpec, *, replace_: bool = False) -> list[str]:
    return [f"{b.name}:{b.change}" for b in changes(before, after, replace=replace_)]


def test_a_new_tenant_breaks_nothing() -> None:
    assert changes(None, BASE, replace=False) == []


def test_additions_and_relaxations_are_not_breaking() -> None:
    before = replace(BASE, property_schema=_schema(
        "strict", status={"type": "string", "enum": ["a", "b"], "required": True},
        size={"type": "integer", "minimum": 1, "maximum": 9},
    ))
    after = replace(
        before,
        vertex_labels=(*BASE.vertex_labels, "Person"),
        property_schema=_schema(
            "strict", status={"type": "string", "enum": ["a", "b", "c"]},
            size={"type": "number", "minimum": 0}, note={"type": "string", "recommended": True},
        ),
    )
    assert _codes(before, after) == []


def test_left_out_labels_break_only_when_replacing() -> None:
    after = replace(BASE, vertex_labels=("Doc", "Report"), edge_labels=("LINKS_TO",),
                    edge_endpoints=())
    assert _codes(BASE, after) == []
    assert _codes(BASE, after, replace_=True) == ["Tag:label_removed", "TAGGED:label_removed"]


def test_removed_index_hierarchy_and_rule_break_reads_and_calls() -> None:
    before = replace(BASE, resolve_rules=(("LINKS_TO", {"src_has_array": "links"}),))
    after = replace(BASE, search_indexes={}, label_hierarchy=())
    assert _codes(before, after) == [
        "None:search_index_removed", "Doc:hierarchy_removed", "LINKS_TO:resolve_rule_removed",
    ]


def test_strict_endpoints_break_when_turned_on_or_a_pair_is_removed() -> None:
    on = replace(BASE, strict_endpoints=True)
    assert _codes(BASE, on) == ["None:strict_endpoints_on"]
    assert _codes(on, replace(on, edge_endpoints=())) == ["TAGGED:endpoint_removed"]
    assert _codes(replace(on, strict_endpoints=False), replace(BASE, edge_endpoints=())) == []


def test_tighter_rules_break_only_where_the_mode_is_strict() -> None:
    before = replace(BASE, property_schema=_schema(
        "strict", status={"type": "string", "enum": ["a", "b"]}, size={"type": "integer"},
        code={"type": "any"},
    ))
    tighter = _schema(
        "strict", status={"type": "string", "enum": ["a"], "required": True},
        size={"type": "integer", "minimum": 1, "maximum": 9},
        code={"type": "string", "pattern": "[A-Z]+"}, owner={"type": "string", "required": True},
    )
    assert sorted(_codes(before, replace(BASE, property_schema=tighter))) == sorted([
        "Doc:property_required", "Doc:enum_narrowed", "Doc:minimum_raised", "Doc:maximum_lowered",
        "Doc:type_changed", "Doc:pattern_changed", "Doc:property_added_required",
    ])
    warned = {**tighter, "mode": "warn"}
    assert _codes(replace(before, property_schema={**before.property_schema, "mode": "warn"}),
                  replace(BASE, property_schema=warned)) == []


def test_rules_reach_children_and_strict_mode_is_one_change() -> None:
    before = replace(BASE, property_schema=_schema("warn", name={"type": "string"}))
    after = replace(BASE, property_schema=_schema("strict", name={"type": "string"}))
    found = changes(before, after, replace=False)
    assert {(b.name, b.change) for b in found} == {
        ("Doc", "mode_strict"), ("Report", "mode_strict"),
    }
    assert "the mode was warn" in found[0].detail


def test_rejecting_undeclared_keys_is_breaking() -> None:
    before = replace(BASE, property_schema=_schema("strict", name={"type": "string"}))
    entry = {"undeclared": "reject", "properties": {"name": {"type": "string"}}}
    after = replace(BASE, property_schema={"mode": "strict", "vertex": {"Doc": entry}})
    assert "Doc:undeclared_rejected" in _codes(before, after)

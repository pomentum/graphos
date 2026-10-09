"""What a registration changed, as recorded in the change feed."""

from __future__ import annotations

from graphos.bootstrap import TenantSpec
from graphos.schema_changes import diff, size


def _spec(**fields: object) -> TenantSpec:
    return TenantSpec(name="t", **fields)  # type: ignore[arg-type]


def test_a_new_tenant_is_recorded_with_everything_it_declares() -> None:
    after = _spec(
        vertex_labels=("A",), edge_labels=("R",),
        property_indexes=({"label": "A", "properties": ["k"], "kind": "equality",
                           "unique": True, "name": None},),
        generated_columns=({"label": "A", "column": "k", "property": "k", "type": "text"},),
    )
    detail = diff(None, after)
    assert detail == {
        "added": {
            "vertex_labels": ["A"], "edge_labels": ["R"],
            "property_indexes": ["A(k) unique"], "generated_columns": ["A.k (text, from k)"],
        },
        "registered": True,
    }
    assert size(detail) == 4


def test_an_unchanged_spec_records_nothing() -> None:
    spec = _spec(
        vertex_labels=("A", "B"), edge_labels=("R",), edge_endpoints=(("R", "A", "B"),),
        resolve_rules=(("R", {"match": {"a": "b"}}),),
    )
    assert diff(spec, _spec(**{f: getattr(spec, f) for f in (
        "vertex_labels", "edge_labels", "edge_endpoints", "resolve_rules",
    )})) is None


def test_additions_removals_and_switches() -> None:
    before = _spec(vertex_labels=("A", "B"), label_hierarchy=(("B", "A"),))
    after = _spec(
        vertex_labels=("A", "C"), resolve_rules=(("R", {"x": 1}), ("R", {"x": 2})),
        strict_endpoints=True,
    )
    detail = diff(before, after)
    assert detail == {
        "added": {"vertex_labels": ["C"], "resolve_rules": ["R"]},
        "removed": {"vertex_labels": ["B"], "label_hierarchy": [["B", "A"]]},
        "strict_endpoints": [False, True],
    }
    # C, R (its two rules count once: by edge type), B, the hierarchy edge, strict.
    assert size(detail) == 5

"""Property schema: the contract, compiling definitions down the hierarchy, and
checking rows against them (no database)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from graphos import schema_document
from graphos.bootstrap import TenantSpec
from graphos.contracts.property_schema import PropertySchemaSpec
from graphos.properties import SchemaError, compile_schema, count, definitions_count
from graphos.schema_changes import diff

PROVENANCE = {"properties": {
    "source": {"type": "string", "required": True},
    "loaded_at": {"type": "string", "format": "date-time", "required": True},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
}}
SCHEMA: dict[str, Any] = {
    "mode": "strict",
    "vertex": {
        "*": PROVENANCE,
        "Person": {"description": "Someone", "undeclared": "warn", "properties": {
            "name": {"type": "string", "required": True},
            "email": {"type": "string", "pattern": "[^@]+@[^@]+", "recommended": True},
            "id": {"pattern": "p-[0-9]+"},
        }},
        "Employee": {"properties": {
            "grade": {"type": "string", "enum": ["junior", "senior"]},
            "email": {"required": True},
        }},
    },
    "edge": {"WROTE": {"properties": {
        "pages": {"type": "array", "items": {"type": "integer"}},
    }}},
}
HIERARCHY = {"Employee": "Person"}
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def _policies(schema: dict[str, Any] = SCHEMA) -> Any:
    return compile_schema(schema, ["Person", "Employee", "Doc"], ["WROTE"], HIERARCHY)


def _rules(violations: list[Any]) -> list[str]:
    return sorted(v.key + (" (warning)" if v.severity == "warning" else "") for v in violations)


GOOD = {"source": "crm", "loaded_at": "2026-10-15T09:00:00Z", "name": "Ada"}


def test_the_contract_refuses_what_cannot_be_checked() -> None:
    PropertySchemaSpec.model_validate(SCHEMA)
    for bad in (
        {"vertex": {"Person": {"properties": {"graphos_rank": {}}}}},
        {"vertex": {"Person": {"properties": {"community_id": {}}}}},
        {"vertex": {"Person": {"properties": {"id": {"type": "string"}}}}},
        {"vertex": {"Person": {"properties": {"name": {"pattern": "("}}}}},
        {"vertex": {"Person": {"properties": {"age": {"type": "integer", "enum": ["x"]}}}}},
        {"vertex": {"Person": {"properties": {"age": {"type": "string", "minimum": 1}}}}},
        {"vertex": {"Person": {"properties": {"n": {"required": True, "recommended": True}}}}},
        {"vertex": {"Person": {"properties": {"n": {"type": "string", "items": {}}}}}},
        {"vertex": {"Person": {"properties": {"n": {"kind": "string"}}}}},
        {"vertex": {"*": {"description": "every label"}}},
        {"vertex": {"person": {}}},
        {"mode": "loud"},
    ):
        with pytest.raises(ValidationError):
            PropertySchemaSpec.model_validate(bad)


def test_definitions_merge_down_the_hierarchy_and_may_only_narrow() -> None:
    employee = _policies().for_vertex("Employee")
    assert set(employee.properties) == {
        "source", "loaded_at", "confidence", "name", "email", "id", "grade",
    }
    email = employee.properties["email"]
    assert email.spec["type"] == "string" and email.spec["required"]   # inherited, made required
    assert "recommended" not in email.spec and email.source == "Employee"
    assert employee.undeclared == "warn" and employee.mode == "strict"
    # A label without an entry gets `*`; an unknown one too.
    assert set(_policies().for_vertex("Doc").properties) == set(PROVENANCE["properties"])
    assert set(_policies().for_vertex("Later").properties) == set(PROVENANCE["properties"])
    changed = {"vertex": {"Person": {"properties": {"name": {"type": "string"}}},
                          "Employee": {"properties": {"name": {"type": "integer"}}}}}
    with pytest.raises(SchemaError, match="not change its type"):
        _policies(changed)


def test_rows_are_checked_rule_by_rule() -> None:
    person = _policies().for_vertex("Person")
    assert _rules(person.check(GOOD, row_id="p-1")) == ["Person.email:recommended (warning)"]
    bad = {"source": 7, "loaded_at": "2026-10-15T09:00:00", "confidence": 1.5, "name": None,
           "email": "nobody", "nmae": "Ada", "graphos_pagerank": 0.1, "community_id": 3}
    assert _rules(person.check(bad, row_id="x-1")) == [
        "*.confidence:maximum", "*.loaded_at:format", "*.source:type",
        "Person.email:pattern", "Person.id:pattern", "Person.name:required",
        "Person.nmae:undeclared (warning)",
    ]
    employee = _policies().for_vertex("Employee")
    lead = {**GOOD, "grade": "lead", "email": "a@b"}
    assert _rules(employee.check(lead)) == ["Employee.grade:enum"]
    wrote = _policies().for_edge("WROTE")
    assert _rules(wrote.check({**GOOD, "pages": [1, "2"]})) == ["WROTE.pages:items"]
    assert _rules(wrote.check({**GOOD, "pages": [1, True]})) == ["WROTE.pages:items"]  # not an int


def test_kinds_are_strict_and_null_is_absent() -> None:
    doc = compile_schema({"mode": "warn", "vertex": {"Doc": {"properties": {
        "n": {"type": "number"}, "i": {"type": "integer"}, "b": {"type": "boolean"},
        "d": {"type": "string", "format": "date"},
    }}}}, ["Doc"], [], {}).for_vertex("Doc")
    assert _rules(doc.check({"n": True, "i": 1.0, "b": 1, "d": "2026-13-01"})) == [
        "Doc.b:type", "Doc.d:format", "Doc.i:type", "Doc.n:type",
    ]
    assert doc.check({"n": 1, "i": 2, "b": False, "d": "2026-10-15"}) == []


def test_mode_off_checks_nothing_and_counts_by_rule() -> None:
    off = _policies({**SCHEMA, "mode": "off"}).for_vertex("Person")
    assert off.check({"name": 1}) == []
    per_label = _policies({**SCHEMA, "mode": "off", "vertex": {
        **SCHEMA["vertex"], "Person": {**SCHEMA["vertex"]["Person"], "mode": "warn"}}})
    assert per_label.for_vertex("Person").checks and not per_label.for_vertex("Doc").checks
    errors, warnings = count(_policies().for_vertex("Person").check({"nmae": 1}))
    assert errors == {"*.source:required": 1, "*.loaded_at:required": 1, "Person.name:required": 1}
    assert warnings == {"Person.email:recommended": 1, "Person.nmae:undeclared": 1}
    assert definitions_count(SCHEMA) == 9


def _spec(schema: dict[str, Any]) -> TenantSpec:
    return TenantSpec(name="acme", vertex_labels=("Person", "Employee", "Doc"),
                      edge_labels=("WROTE",), label_hierarchy=tuple(HIERARCHY.items()),
                      property_schema=schema)


def test_merge_document_and_change_feed() -> None:
    stored = _spec(SCHEMA)
    incoming = _spec({"vertex": {"Doc": {"description": "A document"}}})
    after = schema_document.merged(
        stored, incoming, replace=False, endpoints_stated=False,
        schema_removed=(("edge", "WROTE"),),
    )
    assert after.property_schema["mode"] == "strict"                  # not stated: kept
    assert set(after.property_schema["vertex"]) == {"*", "Person", "Employee", "Doc"}
    assert "edge" not in after.property_schema                        # null removed the entry
    assert diff(stored, after) == {
        "added": {"property_schema": ["vertex:Doc"]},
        "removed": {"property_schema": ["edge:WROTE"]},
    }
    relaxed = _spec({**SCHEMA, "mode": "warn"})
    assert diff(stored, relaxed) == {"property_mode": ["strict", "warn"]}

    doc = schema_document.document(stored, now=NOW)
    assert doc["_export"]["version"] == 2
    assert list(doc["property_schema"]["vertex"]) == ["*", "Employee", "Person"]   # sorted
    assert schema_document.document(_spec({}), now=NOW)["_export"]["version"] == 1

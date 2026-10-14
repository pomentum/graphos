"""The schema document (``graphos.schema``): built from a spec, read back as a
registration body, and what a registration would leave (no database)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from graphos import schema_document
from graphos.bootstrap import TenantSpec
from graphos.contracts.tenants import TenantSpecBody
from graphos.schema_changes import diff

NOW = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)
RULE_A = {"src_has_array": "tags", "dst_value_from": "name"}
RULE_B = {"src_has_array": "topics", "dst_value_from": "name"}
INDEX = {"label": "Doc", "properties": ["kind"], "kind": "equality", "unique": False,
         "name": None}
COLUMN = {"label": "Doc", "column": "kind", "property": "kind", "type": "text"}

SPEC = TenantSpec(
    name="acme",
    vertex_labels=("Person", "Doc", "Employee"),
    edge_labels=("TAGGED", "AUTHORED"),
    property_indexes=(INDEX,),
    generated_columns=(COLUMN,),
    resolve_rules=(("TAGGED", RULE_B), ("TAGGED", RULE_A)),
    label_hierarchy=(("Employee", "Person"),),
    meta={"z": "1", "a": "2"},
    search_indexes={"names": {"labels": ["*"], "properties": ["name"],
                              "analyzer": "identifier", "language": "simple"}},
    edge_endpoints=(("AUTHORED", "Person", "Doc"), ("AUTHORED", "Employee", "Doc")),
    strict_endpoints=True,
)


def test_the_document_is_a_registration_body_and_says_what_it_is() -> None:
    doc = schema_document.document(SPEC, now=NOW)
    assert doc["_export"] == {
        "format": "graphos.schema", "version": 1, "graphos": doc["_export"]["graphos"],
        "tenant": "acme", "exported_at": "2026-10-12T09:30:00Z",
    }
    assert list(doc)[1:] == [
        "name", "vertex_labels", "edge_labels", "label_hierarchy", "edge_endpoints",
        "strict_endpoints", "search_indexes", "property_indexes", "generated_columns", "meta",
        "display",
    ]
    # Sorted, and one entry per resolve rule beside the plain names.
    assert doc["vertex_labels"] == ["Doc", "Employee", "Person"]
    assert doc["edge_labels"] == [
        "AUTHORED",
        {"name": "TAGGED", "resolve_rule": RULE_A},
        {"name": "TAGGED", "resolve_rule": RULE_B},
    ]
    assert doc["edge_endpoints"] == {"AUTHORED": [
        {"src": "Employee", "dst": "Doc"}, {"src": "Person", "dst": "Doc"},
    ]}
    assert list(doc["meta"]) == ["a", "z"]
    body = TenantSpecBody.model_validate(doc)
    assert body.strict_endpoints is True
    assert [e.resolve_rule for e in body.edge_labels if e.name == "TAGGED"] == [RULE_A, RULE_B]


def test_the_same_schema_gives_the_same_document() -> None:
    shuffled = TenantSpec(
        name="acme",
        vertex_labels=tuple(reversed(SPEC.vertex_labels)),
        edge_labels=tuple(reversed(SPEC.edge_labels)),
        property_indexes=SPEC.property_indexes,
        generated_columns=SPEC.generated_columns,
        resolve_rules=tuple(reversed(SPEC.resolve_rules)),
        label_hierarchy=SPEC.label_hierarchy,
        meta=dict(reversed(list(SPEC.meta.items()))),
        search_indexes=SPEC.search_indexes,
        edge_endpoints=tuple(reversed(SPEC.edge_endpoints)),
        strict_endpoints=True,
    )
    assert schema_document.document(shuffled, now=NOW) == schema_document.document(SPEC, now=NOW)


@pytest.mark.parametrize(
    ("header", "needle"),
    [
        ({"format": "graphos.schema", "version": 2, "graphos": "9.0.0"}, "version 2"),
        ({"format": "something.else", "version": 1}, "something.else"),
        ({"version": "1"}, "positive integer"),
        ("graphos.schema", "object"),
    ],
)
def test_a_header_from_elsewhere_or_later_is_refused(header: object, needle: str) -> None:
    with pytest.raises(ValidationError, match=needle):
        TenantSpecBody.model_validate({"_export": header, "name": "acme"})


def test_a_body_without_a_header_is_accepted() -> None:
    assert TenantSpecBody.model_validate({"name": "acme"}).name == "acme"


# ── merged: what a registration would leave ────────────────────────────────


def test_merge_adds_and_keeps() -> None:
    incoming = TenantSpec(
        name="acme", vertex_labels=("Invoice",), meta={"a": "3"},
        search_indexes={"names": {"labels": ["Doc"], "properties": ["title"],
                                  "analyzer": "identifier", "language": "simple"}},
        edge_endpoints=(("AUTHORED", "Person", "Doc"),),
    )
    after = schema_document.merged(SPEC, incoming, replace=False, endpoints_stated=True)
    assert set(after.vertex_labels) == {"Doc", "Employee", "Person", "Invoice"}
    assert after.meta == {"z": "1", "a": "3"}
    assert after.search_indexes["names"]["properties"] == ["title"]   # replaced by name
    assert set(after.edge_endpoints) == set(SPEC.edge_endpoints)      # added, never removed
    assert after.strict_endpoints is True                             # left out: as stored
    # The change feed names search indexes, so a redefinition is not listed.
    # meta keys by name: the text of one changed.
    assert diff(SPEC, after) == {
        "added": {"vertex_labels": ["Invoice"]}, "changed": {"meta": ["a"]},
    }


def test_a_null_meta_key_is_removed_by_a_merge() -> None:
    body = TenantSpecBody.model_validate({"name": "acme", "meta": {"z": None, "b": "new"}})
    assert body.meta == {"z": None, "b": "new"}
    incoming = TenantSpec(name="acme", meta={"b": "new"})
    after = schema_document.merged(
        SPEC, incoming, replace=False, endpoints_stated=False, meta_removed=("z",),
    )
    assert after.meta == {"a": "2", "b": "new"}
    assert diff(SPEC, after) == {"added": {"meta": ["b"]}, "removed": {"meta": ["z"]}}
    # Replacing keeps only the keys sent (a null one is simply left out).
    replaced = schema_document.merged(
        SPEC, incoming, replace=True, endpoints_stated=False, meta_removed=("z",),
    )
    assert replaced.meta == {"b": "new"}


def test_replace_keeps_declared_pairs_unless_stated() -> None:
    incoming = TenantSpec(name="acme", vertex_labels=("Doc",), strict_endpoints=None)
    kept = schema_document.merged(SPEC, incoming, replace=True, endpoints_stated=False)
    assert kept.vertex_labels == ("Doc",)
    assert kept.edge_endpoints == SPEC.edge_endpoints and kept.strict_endpoints is True
    dropped = schema_document.merged(SPEC, incoming, replace=True, endpoints_stated=True)
    assert dropped.edge_endpoints == ()
    removed = diff(SPEC, dropped)
    assert removed is not None
    assert set(removed["removed"]["vertex_labels"]) == {"Employee", "Person"}


def test_a_new_tenant_is_the_incoming_spec() -> None:
    after = schema_document.merged(None, SPEC, replace=False, endpoints_stated=True)
    assert after == SPEC
    assert diff(None, after)["registered"] is True  # type: ignore[index]


def test_display_entries_are_checked() -> None:
    body = TenantSpecBody.model_validate({"name": "acme", "display": {
        "vertex": {"Person": {"color": "#5fa584", "caption": "full_name", "size_by":
                              "graphos_pagerank", "icon": "person"}, "Doc": None},
        "edge": {"AUTHORED": {"caption": "role"}},
    }})
    assert body.display.vertex["Person"] is not None
    assert body.display.vertex["Person"].color == "#5FA584"          # kept upper case
    assert body.display.vertex["Doc"] is None                         # a removal
    for bad in (
        {"vertex": {"Person": {"color": "green"}}},
        {"vertex": {"Person": {"caption": "full name"}}},
        {"vertex": {"Person": {"icon": "rocket"}}},
        {"vertex": {"person": {"color": "#5FA584"}}},
        {"vertex": {"Person": {"colour": "#5FA584"}}},
        {"edge": {"AUTHORED": {"icon": "link"}}},                     # icons are for vertices
        {"nodes": {}},
    ):
        with pytest.raises(ValidationError):
            TenantSpecBody.model_validate({"name": "acme", "display": bad})


def test_display_merges_by_entry() -> None:
    stored = TenantSpec(name="acme", vertex_labels=("Person", "Doc"), edge_labels=("AUTHORED",),
                        display={"vertex": {"Person": {"color": "#5FA584"},
                                            "Doc": {"icon": "document"}},
                                 "edge": {"AUTHORED": {"color": "#8D97AE"}}})
    incoming = TenantSpec(name="acme", display={"vertex": {"Person": {"caption": "full_name"}}})
    after = schema_document.merged(
        stored, incoming, replace=False, endpoints_stated=False,
        display_removed=(("edge", "AUTHORED"),),
    )
    assert after.display == {"vertex": {"Person": {"caption": "full_name"},
                                        "Doc": {"icon": "document"}}}
    assert diff(stored, after) == {
        "removed": {"display": ["edge:AUTHORED"]}, "changed": {"display": ["vertex:Person"]},
    }
    doc = schema_document.document(after, now=NOW)
    assert doc["display"] == {"vertex": {"Doc": {"icon": "document"},
                                         "Person": {"caption": "full_name"}}}


def test_contrast_against_the_canvases() -> None:
    assert schema_document.contrast("#000000", "#FFFFFF") == pytest.approx(21)
    # The UI's own category colours read on both canvases.
    for color in ("#C9825B", "#5FA584", "#8D97AE"):
        assert schema_document.contrast(color, "#E3E5E9") >= 1.6
        assert schema_document.contrast(color, "#26292E") >= 1.6

"""Renaming a vertex label, an edge type or a property (``graphos.schema_rename``,
ADR-0009) without a database: what is refused and why, what the spec looks like
afterwards, what a dry run reports, and what reaches the database, over a faked
connection (``test_schema_rename_db.py`` renames for real)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import asyncpg
import pytest
from fastapi import HTTPException

from graphos import (
    changes,
    declared,
    jobs,
    properties,
    renames,
    schema_rename,
    schema_version,
    writes,
)
from graphos.bootstrap import TenantSpec, spec_from_json, spec_to_json
from graphos.lifecycle import TenantBusyError
from graphos.schema_rename import RenameError

SPEC = TenantSpec(
    name="acme",
    vertex_labels=("Person", "Employee", "Team", "Doc"),
    edge_labels=("KNOWS", "MEMBER_OF", "WROTE"),
    label_hierarchy=(("Employee", "Person"),),
    property_indexes=({"label": "Person", "properties": ["mail"]},
                      {"label": "Doc", "properties": ["kind"]}),
    generated_columns=({"label": "Doc", "property": "pages", "column": "pages_n", "type": "int"},),
    search_indexes={
        "people": {"labels": ["Person", "Employee"], "properties": ["nick", "bio"]},
        "docs": {"labels": ["Doc"], "properties": ["title"]},
    },
    resolve_rules=(
        ("MEMBER_OF", {"src_has_array": "teams", "dst_value_from": "slug",
                       "require_dst_label": ["Team"]}),
    ),
    edge_endpoints=(("MEMBER_OF", "Person", "Team"), ("WROTE", "Person", "Doc"),
                    ("KNOWS", "Person", "Person")),
    display={"vertex": {"Person": {"color": "#112233", "caption": "nick", "size_by": "nick"}},
             "edge": {"KNOWS": {"color": "#445566"}}},
    property_schema={"vertex": {"Person": {"properties": {
        "nick": {"type": "string"}, "mail": {"type": "string"},
    }}}},
    deprecated={"vertex": {"Person": {"properties": {"handle": {"replaced_by": "nick"},
                                                     "nick": {"note": "short"}}}}},
)


# ── Label and edge type: what is refused ───────────────────────────────────


@pytest.mark.parametrize(("kind", "old", "new", "why"), [
    ("label", "Robot", "Machine", "'Robot' is not a vertex label of tenant 'acme'"),
    ("label", "KNOWS", "LIKES", "'KNOWS' is not a vertex label"),
    ("edge_type", "Person", "Human", "'Person' is not a edge type"),
    ("label", "Person", "person", "must match"),
    ("label", "Person", "graphos_person", "must match"),
    ("label", "Person", "_ag_label_vertex", "must match"),
    ("label", "Person", "Has space", "must match"),
    ("label", "Person", "P" * 64, "must match"),
    ("label", "Person", "", "must match"),
    ("label", "Person", "Person", "the new name is the old one"),
    ("label", "Person", "Team", "'Team' is already a label or edge type"),
    ("label", "Person", "KNOWS", "'KNOWS' is already a label or edge type"),
    ("edge_type", "KNOWS", "WROTE", "'WROTE' is already a label or edge type"),
    ("edge_type", "KNOWS", "Doc", "'Doc' is already a label or edge type"),
])
def test_what_a_label_or_type_rename_refuses(kind: str, old: str, new: str, why: str) -> None:
    with pytest.raises(RenameError, match=why):
        schema_rename.check(SPEC, kind, old, new)


def test_a_rename_error_is_a_value_error() -> None:
    assert issubclass(RenameError, ValueError)


def test_the_longest_allowed_name_is_63_characters() -> None:
    schema_rename.check(SPEC, "label", "Person", "P" * 63)


def test_a_name_taken_by_another_labels_sequence_is_refused() -> None:
    with pytest.raises(RenameError):
        schema_rename.check(SPEC, "label", "Doc", "Person_id_seq")


# ── Label and edge type: the spec afterwards ───────────────────────────────


def test_a_label_rename_carries_its_hierarchy_columns_rules_and_endpoints() -> None:
    after = schema_rename.renamed_spec(SPEC, "label", "Person", "Human", {})
    assert after.vertex_labels == ("Human", "Employee", "Team", "Doc")
    assert after.label_hierarchy == (("Employee", "Human"),)
    assert [x["label"] for x in after.property_indexes] == ["Human", "Doc"]
    assert after.search_indexes["people"]["labels"] == ["Human", "Employee"]
    assert after.search_indexes["docs"] == SPEC.search_indexes["docs"]
    assert after.edge_endpoints == (("MEMBER_OF", "Human", "Team"), ("WROTE", "Human", "Doc"),
                                    ("KNOWS", "Human", "Human"))
    assert after.display["vertex"] == {"Human": SPEC.display["vertex"]["Person"]}
    assert after.property_schema["vertex"] == {"Human": SPEC.property_schema["vertex"]["Person"]}
    assert after.deprecated["vertex"] == {"Human": SPEC.deprecated["vertex"]["Person"]}
    assert after.edge_labels == SPEC.edge_labels


def test_renaming_a_parent_label_keeps_its_children_below_it() -> None:
    after = schema_rename.renamed_spec(SPEC, "label", "Person", "Human", {})
    assert schema_rename.below(after, "Human") == ["Human", "Employee"]
    after = schema_rename.renamed_spec(SPEC, "label", "Employee", "Staff", {})
    assert after.label_hierarchy == (("Staff", "Person"),)


def test_a_label_named_by_a_resolve_rule_is_renamed_in_it() -> None:
    after = schema_rename.renamed_spec(SPEC, "label", "Team", "Squad", {})
    assert after.resolve_rules == (
        ("MEMBER_OF", {"src_has_array": "teams", "dst_value_from": "slug",
                       "require_dst_label": ["Squad"]}),
    )
    assert after.generated_columns == SPEC.generated_columns


def test_a_label_rename_leaves_the_original_spec_untouched() -> None:
    before = json.loads(spec_to_json(SPEC))
    schema_rename.renamed_spec(SPEC, "label", "Person", "Human", {"kind": "label"})
    assert json.loads(spec_to_json(SPEC)) == before


def test_an_edge_type_rename_touches_no_label() -> None:
    after = schema_rename.renamed_spec(SPEC, "edge_type", "KNOWS", "LIKES", {})
    assert after.edge_labels == ("LIKES", "MEMBER_OF", "WROTE")
    assert after.vertex_labels == SPEC.vertex_labels
    assert after.edge_endpoints[-1] == ("LIKES", "Person", "Person")
    assert after.display["edge"] == {"LIKES": {"color": "#445566"}}
    assert after.property_indexes == SPEC.property_indexes


def test_each_rename_is_recorded_in_order() -> None:
    first = {"kind": "label", "from": "Team", "to": "Squad"}
    second = {"kind": "edge_type", "from": "MEMBER_OF", "to": "IN_SQUAD"}
    after = schema_rename.renamed_spec(SPEC, "label", "Team", "Squad", first)
    after = schema_rename.renamed_spec(after, "edge_type", "MEMBER_OF", "IN_SQUAD", second)
    assert after.renames == (first, second)
    assert renames.why_unknown(after, "Team") is not None
    assert renames.why_unknown(after, "MEMBER_OF") is not None


def test_a_label_renamed_and_renamed_back_is_a_label_again() -> None:
    there = {"kind": "label", "from": "Team", "to": "Squad", "at": "2026-10-01T00:00:00+00:00"}
    back = {"kind": "label", "from": "Squad", "to": "Team", "at": "2026-10-02T00:00:00+00:00"}
    after = schema_rename.renamed_spec(SPEC, "label", "Team", "Squad", there)
    schema_rename.check(after, "label", "Squad", "Team")
    after = schema_rename.renamed_spec(after, "label", "Squad", "Team", back)
    assert "Team" in after.vertex_labels
    assert renames.why_unknown(after, "Team") is None
    assert renames.why_unknown(after, "Squad") == (
        "'Squad' was renamed to 'Team' on 2026-10-02: use the new name"
    )


def test_a_name_renamed_twice_answers_where_it_went_last() -> None:
    after = schema_rename.renamed_spec(
        SPEC, "label", "Team", "Squad", {"kind": "label", "from": "Team", "to": "Squad"},
    )
    after = schema_rename.renamed_spec(
        after, "label", "Squad", "Crew", {"kind": "label", "from": "Squad", "to": "Crew"},
    )
    assert "'Crew'" in (renames.why_unknown(after, "Team") or "")


def test_a_deprecated_label_replaced_by_the_renamed_one_now_names_the_new_one() -> None:
    spec = replace(SPEC, vertex_labels=(*SPEC.vertex_labels, "Squad"),
                   deprecated={"vertex": {"Squad": {"deprecated": True, "replaced_by": "Team"}}})
    after = schema_rename.renamed_spec(spec, "label", "Team", "Crew", {})
    assert after.deprecated["vertex"]["Squad"]["replaced_by"] == "Crew"
    assert "use 'Crew'" in properties.policies_for(after).for_vertex("Squad").deprecated_label


def test_a_deprecated_label_keeps_its_deprecation_under_the_new_name() -> None:
    spec = replace(SPEC, deprecated={"vertex": {"Team": {"deprecated": True, "note": "old"}}})
    after = schema_rename.renamed_spec(spec, "label", "Team", "Crew", {})
    assert after.deprecated == {"vertex": {"Crew": {"deprecated": True, "note": "old"}}}
    assert properties.policies_for(after).for_vertex("Crew").deprecated_label is not None


def test_a_label_rename_keeps_the_property_renames_made_on_it() -> None:
    spec = replace(SPEC, renames=({"kind": "property", "from": "mail", "to": "email",
                                   "on": "Person", "status": "done"},))
    with pytest.raises(properties.Refused):
        properties.enforce([(properties.policies_for(spec).for_vertex("Person"),
                             {"mail": "a@b"}, "p1", {})])
    after = schema_rename.renamed_spec(spec, "label", "Person", "Human", {})
    with pytest.raises(properties.Refused):
        properties.enforce([(properties.policies_for(after).for_vertex("Human"),
                             {"mail": "a@b"}, "p1", {})])


# ── What a dry run names ───────────────────────────────────────────────────


def test_the_spec_parts_naming_a_label_are_listed() -> None:
    assert set(schema_rename.spec_parts_named(SPEC, "label", "Person")) == {
        "label_hierarchy", "property_indexes", "search_indexes", "display", "property_schema",
        "deprecated",
    }
    assert schema_rename.spec_parts_named(SPEC, "label", "Team") == ["resolve_rules"]
    assert set(schema_rename.spec_parts_named(SPEC, "label", "Doc")) == {
        "property_indexes", "generated_columns", "search_indexes",
    }


def test_the_spec_parts_naming_an_edge_type_are_listed() -> None:
    assert schema_rename.spec_parts_named(SPEC, "edge_type", "MEMBER_OF") == ["resolve_rules"]
    assert schema_rename.spec_parts_named(SPEC, "edge_type", "KNOWS") == ["display"]
    assert schema_rename.spec_parts_named(SPEC, "edge_type", "WROTE") == []


def test_a_label_named_only_as_a_replacement_lists_deprecated_once() -> None:
    spec = replace(SPEC, deprecated={"vertex": {"Squad": {"deprecated": True,
                                                          "replaced_by": "Team"}}})
    assert schema_rename.spec_parts_named(spec, "label", "Team") == [
        "resolve_rules", "deprecated",
    ]


@pytest.mark.parametrize(("query", "named"), [
    ("MATCH (p:Person) RETURN p", True),
    ("MATCH (a)-[:KNOWS]->(b) RETURN b", True),
    ("MATCH (p :Person) RETURN p", True),
    ("MATCH (p:`Person`) RETURN p", True),
    ("MATCH (p:Person_Archive) RETURN p", False),
    ("MATCH (p:PersonX) RETURN p", False),
    ("MATCH (p:Employee) WHERE p.Person = 1 RETURN p", False),
    ("MATCH (p:Employee:Person) RETURN p", True),
])
def test_which_queries_name_a_label(query: str, named: bool) -> None:  # noqa: FBT001
    name = "KNOWS" if "KNOWS" in query else "Person"
    assert schema_rename.mentions(query, name) is named


@pytest.mark.parametrize(("query", "named"), [
    ("MATCH (p:Person) RETURN p.mail", True),
    ("MATCH (p:Person) SET p.`mail` = 'x'", True),
    ("MATCH (p:Person) RETURN p.mailbox", False),
    ("MATCH (p:Person) RETURN p.email", False),
    ("MATCH (p:mail) RETURN p", False),
])
def test_which_queries_read_or_write_a_property(query: str, named: bool) -> None:  # noqa: FBT001
    assert schema_rename.property_mentions(query, "mail") is named


def test_graphos_indexes_named_after_the_label_follow_it() -> None:
    names = ["Person_mail_equality_idx", "idx_Person_slug", "Person_pkey_custom",
             "PersonX_name_idx", "people_bio_trgm", "Personnel_idx"]
    assert schema_rename._index_renames(names, "Person", "Human") == [  # noqa: SLF001
        ("Person_mail_equality_idx", "Human_mail_equality_idx"),
        ("idx_Person_slug", "idx_Human_slug"),
        ("Person_pkey_custom", "Human_pkey_custom"),
    ]


def test_an_index_whose_new_name_would_pass_63_characters_is_left_to_rebuild() -> None:
    long_tail = "x" * 55
    renamed = schema_rename._index_renames(  # noqa: SLF001
        [f"Doc_{long_tail}", f"idx_Doc_{long_tail[:10]}"], "Doc", "Documentation",
    )
    assert renamed == [(f"idx_Doc_{long_tail[:10]}", f"idx_Documentation_{long_tail[:10]}")]


def test_a_label_name_inside_an_index_name_is_replaced_only_in_the_prefix() -> None:
    assert schema_rename._index_renames(  # noqa: SLF001
        ["Team_Team_lead_idx"], "Team", "Crew",
    ) == [("Team_Team_lead_idx", "Crew_Team_lead_idx")]


# ── Property: what is refused ──────────────────────────────────────────────


@pytest.mark.parametrize(("on", "old", "new", "why"), [
    ("Robot", "a", "b", "'Robot' is not a vertex label or edge type of tenant 'acme'"),
    ("Person", "nick", "nick", "the new name is the old one"),
    ("Person", "id", "key", "'id' cannot be renamed: graphos needs it"),
    ("Person", "key", "id", "'id' cannot be renamed"),
    ("Person", "community_id", "group", "'community_id' cannot be renamed"),
    ("Person", "group", "community_id", "'community_id' cannot be renamed"),
    ("Person", "graphos_degree", "degree", "'graphos_degree' cannot be renamed"),
    ("KNOWS", "since", "graphos_since", "'graphos_since' cannot be renamed"),
    ("Doc", "pages", "page_count", r"generated column Doc\.pages_n is computed from 'pages'"),
    ("Team", "slug", "handle", "the resolve rules of MEMBER_OF name 'slug'"),
    ("Person", "teams", "groups", "the resolve rules of MEMBER_OF name 'teams'"),
    ("Employee", "bio", "about", "search index 'people' covers 'bio' on other labels too"),
])
def test_what_a_property_rename_refuses(on: str, old: str, new: str, why: str) -> None:
    with pytest.raises(RenameError, match=why):
        schema_rename.check_property(SPEC, on, old, new)


def test_a_property_rename_on_a_parent_covers_the_labels_below_it() -> None:
    assert schema_rename.check_property(SPEC, "Person", "bio", "about") == ["Person", "Employee"]
    assert schema_rename.check_property(SPEC, "Employee", "rank", "level") == ["Employee"]
    assert schema_rename.check_property(SPEC, "KNOWS", "since", "from_year") == ["KNOWS"]


def test_a_search_index_over_every_label_blocks_renaming_its_property() -> None:
    spec = replace(SPEC, search_indexes={"all": {"properties": ["title"]}})
    with pytest.raises(RenameError, match="search index 'all' covers 'title'"):
        schema_rename.check_property(spec, "Doc", "title", "heading")


def test_a_generated_column_on_a_child_blocks_renaming_on_the_parent() -> None:
    spec = replace(SPEC, generated_columns=(
        {"label": "Employee", "property": "rank", "column": "rank_n", "type": "int"},
    ))
    with pytest.raises(RenameError, match="Employee.rank_n"):
        schema_rename.check_property(spec, "Person", "rank", "level")
    schema_rename.check_property(spec, "Team", "rank", "level")


@pytest.mark.parametrize(("value", "found"), [
    ("slug", True), ("slugs", False), (["a", "slug"], True), ({"slug": 1}, True),
    ({"x": {"y": ["slug"]}}, True), ({"x": 1}, False), (3, False), (None, False),
])
def test_a_resolve_rule_names_a_property_as_a_value_in_a_list_or_as_a_key(
    value: object, found: bool,  # noqa: FBT001
) -> None:
    assert schema_rename._names_in(value, "slug") is found  # noqa: SLF001


def test_the_labels_below_one_come_parents_first_and_once() -> None:
    spec = TenantSpec(name="acme", vertex_labels=("A", "B", "C", "D"),
                      label_hierarchy=(("C", "B"), ("B", "A"), ("D", "A"), ("C", "D")))
    assert schema_rename.below(spec, "A") == ["A", "B", "D", "C"]
    assert schema_rename.below(spec, "C") == ["C"]


def test_a_property_renamed_back_to_its_old_name_can_be_written() -> None:
    # mail -> email done: its definitions say email, its renames record it.
    first = {"kind": "property", "from": "mail", "to": "email", "on": "Person",
             "status": "done"}
    spec = replace(schema_rename.renamed_property_spec(
        SPEC, "Person", ["Person", "Employee"], "mail", "email",
    ), renames=(first,))
    schema_rename.check_property(spec, "Person", "email", "mail")
    entry = {"kind": "property", "from": "email", "to": "mail", "on": "Person",
             "status": "done"}
    after = replace(schema_rename.renamed_property_spec(
        spec, "Person", ["Person", "Employee"], "email", "mail",
    ), renames=(*spec.renames, entry))
    policy = properties.policies_for(after).for_vertex("Person")
    assert policy.check({"mail": "a@b"}) == []


def test_renaming_onto_a_declared_property_is_refused() -> None:
    spec = replace(SPEC, property_schema={"vertex": {"Person": {"properties": {
        "mail": {"type": "string"}, "email": {"type": "integer"},
    }}}})
    with pytest.raises(RenameError):
        schema_rename.check_property(spec, "Person", "mail", "email")


# ── Property: the spec afterwards ──────────────────────────────────────────


def test_a_property_rename_carries_its_definition_indexes_and_styles() -> None:
    entry = {"kind": "property", "from": "nick", "to": "alias", "on": "Person",
             "status": "running"}
    spec = replace(SPEC, renames=(entry,))
    tables = schema_rename.check_property(spec, "Person", "nick", "alias")
    after = schema_rename.renamed_property_spec(spec, "Person", tables, "nick", "alias")
    assert after.property_schema["vertex"]["Person"]["properties"] == {
        "alias": {"type": "string"}, "mail": {"type": "string"},
    }
    assert after.display["vertex"]["Person"] == {
        "color": "#112233", "caption": "alias", "size_by": "alias",
    }
    assert after.search_indexes["people"]["properties"] == ["alias", "bio"]
    assert after.search_indexes["docs"] == SPEC.search_indexes["docs"]
    assert after.renames == ({**entry, "status": "done"},)
    # The deprecation of the old name goes with it.
    assert "nick" not in after.deprecated["vertex"]["Person"]["properties"]


def test_a_property_rename_follows_property_indexes_on_its_labels_only() -> None:
    after = schema_rename.renamed_property_spec(SPEC, "Person", ["Person", "Employee"],
                                                "mail", "email")
    assert after.property_indexes == (
        {"label": "Person", "properties": ["email"]}, {"label": "Doc", "properties": ["kind"]},
    )
    other = schema_rename.renamed_property_spec(SPEC, "Doc", ["Doc"], "mail", "email")
    assert other.property_indexes == SPEC.property_indexes


def test_dropping_the_last_deprecation_of_a_label_drops_its_entry() -> None:
    spec = replace(SPEC, deprecated={"vertex": {"Person": {"properties": {"nick": {}}}}})
    after = schema_rename.renamed_property_spec(spec, "Person", ["Person"], "nick", "alias")
    assert after.deprecated == {}
    deprecated_too = replace(spec, deprecated={"vertex": {"Person": {
        "deprecated": True, "properties": {"nick": {}},
    }}})
    after = schema_rename.renamed_property_spec(deprecated_too, "Person", ["Person"], "nick",
                                                "alias")
    assert after.deprecated == {"vertex": {"Person": {"deprecated": True}}}


def test_only_the_matching_running_rename_is_marked_done() -> None:
    others = (
        {"kind": "property", "from": "nick", "to": "alias", "on": "Doc", "status": "running"},
        {"kind": "property", "from": "nick", "to": "handle", "on": "Person", "status": "failed"},
        {"kind": "label", "from": "nick", "to": "alias"},
    )
    after = schema_rename.renamed_property_spec(replace(SPEC, renames=others), "Person",
                                                ["Person"], "nick", "alias")
    assert after.renames == others


def test_a_deprecated_property_replaced_by_the_renamed_one_now_names_the_new_one() -> None:
    after = schema_rename.renamed_property_spec(SPEC, "Person", ["Person", "Employee"],
                                                "nick", "alias")
    assert after.deprecated["vertex"]["Person"]["properties"]["handle"]["replaced_by"] == "alias"


# ── Fakes at the database boundary ─────────────────────────────────────────


class _Conn:
    """Answers the SQL the renames send, from what it was given; records the rest."""

    def __init__(self, spec: TenantSpec = SPEC, **given: Any) -> None:
        self.spec_text = spec_to_json(spec)
        self.given = {"jobs": 0, "reconcile_free": True, "indexes": [], "saved": [], "rows": 0,
                      "endpoints": 0, "label_rows": 1, "both": 0, "dup_rows": [],
                      "lock_error": None, "insert_error": None, **given}
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.stored: dict[str, Any] | None = None

    def transaction(self) -> _Conn:
        return self

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, sql: str, *args: object) -> str:
        self.executed.append((sql, args))
        if "pg_advisory_xact_lock(" in sql and self.given["lock_error"] is not None:
            raise self.given["lock_error"]
        if "UPDATE ag_catalog.ag_label" in sql:
            return f"UPDATE {self.given['label_rows']}"
        if "UPDATE graphos_meta.tenants" in sql:
            self.stored = json.loads(str(args[1]))
            return "UPDATE 1"
        if "UPDATE graphos_meta.edge_endpoints" in sql:
            return f"UPDATE {self.given['endpoints']}"
        return "OK"

    async def fetchval(self, sql: str, *args: object) -> object:
        if "spec::text" in sql:
            return self.spec_text
        if "graphos_meta.jobs" in sql and "INSERT" in sql:
            if self.given["insert_error"] is not None:
                raise self.given["insert_error"]
            return "job-1"
        if "graphos_meta.jobs" in sql:
            return self.given["jobs"]
        if "pg_try_advisory_xact_lock" in sql:
            return self.given["reconcile_free"]
        if "edge_endpoints" in sql:
            return self.given["endpoints"]
        if "count(*)" in sql:
            return self.given["both"] if len(args) > 1 else self.given["rows"]
        raise AssertionError(sql)

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        if "saved_queries" in sql:
            return [{"name": n, "query": q} for n, q in self.given["saved"]]
        if "pg_get_indexdef" in sql:
            return self.given["dup_rows"]
        if "pg_index" in sql:
            return [{"name": n} for n in self.given["indexes"]]
        if "graphos_meta.edge_endpoints" in sql:
            return [{"edge_type": t, "src_label": s, "dst_label": d}
                    for t, s, d in self.given.get("pairs", [])]
        raise AssertionError(sql)

    def sql(self, part: str) -> list[str]:
        return [s for s, _ in self.executed if part in s]


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self) -> _Conn:
        return self.conn


@pytest.fixture
def quiet(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """The calls a rename makes after its transaction, recorded instead of made."""
    seen: dict[str, list[Any]] = {"remember": [], "ensure": [], "changes": [], "removals": []}

    async def load_spec(_pool: object, _tenant: str) -> None:
        return None

    async def record(_conn: object, **kw: Any) -> None:
        seen["changes"].append(kw)

    async def ensure_after(_pool: object, spec: TenantSpec, new: str, **kw: Any) -> None:
        seen["ensure"].append((spec, new, kw))

    async def record_removal(_conn: object, tenant: str, kind: str, name: str, **kw: Any) -> None:
        seen["removals"].append((tenant, kind, name, kw))

    monkeypatch.setattr(schema_rename, "load_spec", load_spec)
    monkeypatch.setattr(schema_rename, "remember", seen["remember"].append)
    monkeypatch.setattr(schema_rename, "_ensure_after", ensure_after)
    monkeypatch.setattr(schema_rename, "record_removal", record_removal)
    monkeypatch.setattr(changes, "record", record)
    return seen


# ── A dry run ──────────────────────────────────────────────────────────────


async def test_a_dry_run_reports_indexes_endpoints_rows_and_saved_queries() -> None:
    conn = _Conn(indexes=["Person_mail_equality_idx", "people_bio_trgm"], endpoints=3, rows=42,
                 saved=[("friends", "MATCH (p:Person)-[:KNOWS]->(f) RETURN f"),
                        ("docs", "MATCH (d:Doc) RETURN d"), ("empty", None)])
    found = await schema_rename.plan(_Pool(conn), SPEC, "label", "Person", "Human")  # type: ignore[arg-type]
    assert (found.kind, found.old, found.new) == ("label", "Person", "Human")
    assert found.indexes == [("Person_mail_equality_idx", "Human_mail_equality_idx")]
    assert (found.endpoints, found.rows) == (3, 42)
    assert found.saved_queries == ["friends"]
    assert "label_hierarchy" in found.spec_parts
    assert conn.executed == []   # nothing written


async def test_a_dry_run_is_refused_before_touching_the_database() -> None:
    conn = _Conn()
    with pytest.raises(RenameError, match="already a label or edge type"):
        await schema_rename.plan(_Pool(conn), SPEC, "label", "Person", "Team")  # type: ignore[arg-type]


async def test_a_property_dry_run_reports_rows_parts_and_saved_queries() -> None:
    conn = _Conn(rows=5, saved=[("by_mail", "MATCH (p:Person) WHERE p.mail = $m RETURN p"),
                                ("by_name", "MATCH (p:Person) RETURN p.name")])
    found = await schema_rename.plan_property(_Pool(conn), SPEC, "Person", "mail", "email")  # type: ignore[arg-type]
    assert (found.kind, found.rows, found.saved_queries) == ("property", 5, ["by_mail"])
    assert found.spec_parts == ["property_schema", "property_indexes"]
    assert found.warnings == []


async def test_a_property_dry_run_is_refused_when_rows_have_both_names() -> None:
    conn = _Conn(rows=5, both=2)
    with pytest.raises(RenameError, match="2 row\\(s\\) of 'Person' have both 'mail' and 'email'"):
        await schema_rename.plan_property(_Pool(conn), SPEC, "Person", "mail", "email")  # type: ignore[arg-type]


async def test_renaming_name_on_a_label_warns_about_search_without_an_index() -> None:
    found = await schema_rename.plan_property(_Pool(_Conn()), SPEC, "Team", "name", "title")  # type: ignore[arg-type]
    assert len(found.warnings) == 1
    assert "property='title'" in found.warnings[0]
    on_edge = await schema_rename.plan_property(_Pool(_Conn()), SPEC, "KNOWS", "name", "title")  # type: ignore[arg-type]
    assert on_edge.warnings == []


# ── A label or type renamed ────────────────────────────────────────────────


async def test_a_label_rename_renames_the_table_sequence_default_and_indexes(
    quiet: dict[str, list[Any]],
) -> None:
    conn = _Conn(indexes=["Person_mail_equality_idx"], endpoints=2,
                 saved=[("q", "MATCH (p:Person) RETURN p")])
    found = await schema_rename.rename(_Pool(conn), SPEC, "label", "Person", "Human", by="ops")  # type: ignore[arg-type]
    assert conn.sql("lock_timeout")
    assert conn.sql('ALTER TABLE "acme"."Person" RENAME TO "Human"')
    assert conn.sql('ALTER SEQUENCE "acme"."Person_id_seq" RENAME TO "Human_id_seq"')
    assert any("'Human'::name" in s and "Human_id_seq" in s for s in conn.sql("SET DEFAULT"))
    assert conn.sql('ALTER INDEX "acme"."Person_mail_equality_idx" RENAME TO '
                    '"Human_mail_equality_idx"')
    assert conn.sql("graphos_meta.node_properties") and conn.sql("community_map")
    assert found.indexes == [("Person_mail_equality_idx", "Human_mail_equality_idx")]
    assert (found.endpoints, found.saved_queries) == (2, ["q"])
    # The stored spec is the renamed one, with the rename recorded.
    assert conn.stored is not None
    assert "Human" in conn.stored["vertex_labels"]
    assert conn.stored["renames"][-1]["from"] == "Person"
    assert conn.stored["renames"][-1]["by"] == "ops"
    assert quiet["changes"][0]["detail"] == {
        "renamed": {"kind": "label", "from": "Person", "to": "Human"},
    }
    assert conn.sql("pg_notify")
    assert quiet["remember"][0].vertex_labels[0] == "Human"
    assert quiet["ensure"][0][1] == "Human"


async def test_an_edge_type_rename_moves_its_pairs_and_community_links(
    quiet: dict[str, list[Any]],
) -> None:
    conn = _Conn(endpoints=1)
    found = await schema_rename.rename(_Pool(conn), SPEC, "edge_type", "KNOWS", "LIKES", by=None)  # type: ignore[arg-type]
    assert conn.sql("community_links")
    assert not conn.sql("node_properties")
    assert found.endpoints == 1
    assert conn.sql("pg_try_advisory_xact_lock") == []   # fetched, not executed


async def test_a_rename_rechecks_the_spec_as_stored_now(quiet: dict[str, list[Any]]) -> None:
    # Another replica registered Human meanwhile.
    stored = replace(SPEC, vertex_labels=(*SPEC.vertex_labels, "Human"))
    conn = _Conn(stored)
    with pytest.raises(RenameError, match="'Human' is already a label"):
        await schema_rename.rename(_Pool(conn), SPEC, "label", "Person", "Human", by=None)  # type: ignore[arg-type]
    assert not conn.sql("ALTER TABLE")


async def test_a_rename_is_refused_while_jobs_run_on_the_tenant(
    quiet: dict[str, list[Any]],
) -> None:
    conn = _Conn(jobs=2)
    with pytest.raises(TenantBusyError, match="2 job\\(s\\) queued or running"):
        await schema_rename.rename(_Pool(conn), SPEC, "label", "Person", "Human", by=None)  # type: ignore[arg-type]
    assert not conn.sql("ALTER TABLE")


async def test_an_edge_type_rename_is_refused_while_a_reconcile_holds_it(
    quiet: dict[str, list[Any]],
) -> None:
    conn = _Conn(reconcile_free=False)
    with pytest.raises(TenantBusyError, match="a reconcile of 'KNOWS' is running"):
        await schema_rename.rename(_Pool(conn), SPEC, "edge_type", "KNOWS", "LIKES", by=None)  # type: ignore[arg-type]


async def test_tables_in_use_past_the_lock_timeout_answer_busy(
    quiet: dict[str, list[Any]],
) -> None:
    conn = _Conn(lock_error=asyncpg.LockNotAvailableError("lock timeout"))
    with pytest.raises(TenantBusyError, match="tenant 'acme' is in use"):
        await schema_rename.rename(_Pool(conn), SPEC, "label", "Person", "Human", by=None)  # type: ignore[arg-type]


async def test_a_label_missing_from_ages_catalogue_is_refused(
    quiet: dict[str, list[Any]],
) -> None:
    conn = _Conn(label_rows=0)
    with pytest.raises(RenameError, match="'Person' is not a label of the graph 'acme'"):
        await schema_rename.rename(_Pool(conn), SPEC, "label", "Person", "Human", by=None)  # type: ignore[arg-type]
    assert not conn.sql("ALTER TABLE")


async def test_duplicate_indexes_left_by_a_rebuild_are_dropped_keeping_the_first() -> None:
    body = 'ON "acme"."Human" USING btree (x)'
    conn = _Conn(dup_rows=[
        {"name": "Human_a_idx", "body": body, "is_unique": False},
        {"name": "Human_b_idx", "body": body, "is_unique": False},
        {"name": "Human_c_idx", "body": body, "is_unique": True},
        {"name": "Human_d_idx", "body": "ON other", "is_unique": False},
    ])
    dropped = await schema_rename._drop_duplicate_indexes(conn, "acme", "Human")  # type: ignore[arg-type]  # noqa: SLF001
    assert dropped == 1
    assert conn.sql("DROP INDEX") == ['DROP INDEX IF EXISTS "acme"."Human_b_idx";']


async def test_rebuilding_indexes_after_a_rename_never_fails_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("index build failed")

    monkeypatch.setattr(schema_rename, "bootstrap_tenant", broken)
    await schema_rename._ensure_after(  # noqa: SLF001
        _Pool(_Conn()), SPEC, "Human", vertex_graphid_indexes=False,  # type: ignore[arg-type]
    )
    assert "rebuilding indexes failed" in caplog.text


async def test_rebuilding_indexes_after_a_rename_drops_the_leftovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str] = []

    async def done(_pool: object, spec: TenantSpec, **_: object) -> None:
        built.append(spec.name)

    for name in ("bootstrap_tenant", "ensure_search_indexes", "ensure_resolve_rule_indexes"):
        monkeypatch.setattr(schema_rename, name, done if name != "ensure_resolve_rule_indexes"
                            else (lambda _p, *, tenant, spec: done(_p, spec)))
    body = "ON x USING btree (y)"
    conn = _Conn(dup_rows=[{"name": "a", "body": body, "is_unique": False},
                           {"name": "b", "body": body, "is_unique": False}])
    await schema_rename._ensure_after(  # noqa: SLF001
        _Pool(conn), SPEC, "Human", vertex_graphid_indexes=True,  # type: ignore[arg-type]
    )
    assert built == ["acme", "acme", "acme"]
    assert conn.sql("DROP INDEX") == ['DROP INDEX IF EXISTS "acme"."b";']


# ── A property rename started, run and finished ────────────────────────────


async def test_starting_a_property_rename_queues_its_job_and_marks_it_running(
    quiet: dict[str, list[Any]],
) -> None:
    conn = _Conn()
    job = await schema_rename.start_property(_Pool(conn), "acme", "Person", "mail", "email",  # type: ignore[arg-type]
                                             by="ops")
    assert job == "job-1"
    assert conn.stored is not None
    entry = conn.stored["renames"][-1]
    assert {k: entry[k] for k in ("kind", "from", "to", "on", "status", "job_id", "by")} == {
        "kind": "property", "from": "mail", "to": "email", "on": "Person",
        "status": "running", "job_id": "job-1", "by": "ops",
    }
    assert conn.sql("pg_notify")


async def test_a_failed_property_rename_asked_again_is_resumed_not_repeated(
    quiet: dict[str, list[Any]],
) -> None:
    failed = {"kind": "property", "from": "mail", "to": "email", "on": "Person",
              "status": "failed", "job_id": "job-0"}
    conn = _Conn(replace(SPEC, renames=(failed,)))
    await schema_rename.start_property(_Pool(conn), "acme", "Person", "mail", "email", by=None)  # type: ignore[arg-type]
    assert conn.stored is not None
    assert conn.stored["renames"] == [{**failed, "status": "running", "job_id": "job-1"}]


async def test_a_second_property_rename_while_one_runs_is_busy(
    quiet: dict[str, list[Any]],
) -> None:
    conn = _Conn(insert_error=asyncpg.UniqueViolationError("one at a time"))
    with pytest.raises(TenantBusyError, match="a property rename is already queued or running"):
        await schema_rename.start_property(_Pool(conn), "acme", "Person", "mail", "email",  # type: ignore[arg-type]
                                           by=None)


async def test_starting_a_property_rename_is_refused_when_rows_have_both_names(
    quiet: dict[str, list[Any]],
) -> None:
    conn = _Conn(both=1)
    with pytest.raises(RenameError, match="rows of 'Person' have both 'mail' and 'email'"):
        await schema_rename.start_property(_Pool(conn), "acme", "Person", "mail", "email",  # type: ignore[arg-type]
                                           by=None)
    assert conn.stored is None


async def test_a_busy_tenant_cannot_start_a_property_rename(
    quiet: dict[str, list[Any]],
) -> None:
    conn = _Conn(lock_error=asyncpg.LockNotAvailableError("lock timeout"))
    with pytest.raises(TenantBusyError, match="tenant 'acme' is in use; try again"):
        await schema_rename.start_property(_Pool(conn), "acme", "Person", "mail", "email",  # type: ignore[arg-type]
                                           by=None)


@pytest.fixture
def job(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"batches": [3, 2, 0, 1, 0, 0, 0], "progress": [], "marked": [],
                             "finished": [], "spec": SPEC, "finish_error": None}

    async def load_spec(_pool: object, _tenant: str) -> TenantSpec | None:
        return state["spec"]

    async def rename_property_batch(_pool: object, **kw: Any) -> int:
        return state["batches"].pop(0) if state["batches"] else 0

    async def report_progress(_pool: object, done: int, total: int | None) -> None:
        state["progress"].append((done, total))

    async def finish_property(_pool: object, *args: Any) -> None:
        if state["finish_error"] is not None:
            raise state["finish_error"]
        state["finished"].append(args)

    async def mark(_pool: object, *args: Any) -> None:
        state["marked"].append(args)

    monkeypatch.setattr(schema_rename, "load_spec", load_spec)
    monkeypatch.setattr(writes, "rename_property_batch", rename_property_batch)
    monkeypatch.setattr(jobs, "report_progress", report_progress)
    monkeypatch.setattr(schema_rename, "finish_property", finish_property)
    monkeypatch.setattr(schema_rename, "_mark", mark)
    return state


async def test_the_job_rewrites_until_a_whole_pass_finds_nothing(job: dict[str, Any]) -> None:
    conn = _Conn(rows=5)
    answer = await schema_rename.run_property_job(_Pool(conn), "acme",  # type: ignore[arg-type]
                                                  {"on": "Person", "from": "mail", "to": "email"})
    assert answer.rows == 6  # noqa: PLR2004 -- 3 + 2 on Person, then 1 more on a second pass
    assert job["progress"][0] == (0, 5)
    assert job["progress"][-1] == (6, 6)
    assert job["finished"] == [("acme", "Person", "mail", "email", ["Person", "Employee"])]
    assert job["marked"] == []


async def test_a_job_whose_rename_is_no_longer_possible_marks_it_failed(
    job: dict[str, Any],
) -> None:
    job["spec"] = None
    with pytest.raises(RenameError, match="tenant 'acme' is not registered"):
        await schema_rename.run_property_job(_Pool(_Conn()), "acme",  # type: ignore[arg-type]
                                             {"on": "Person", "from": "mail", "to": "email"})
    assert job["marked"] == [("acme", "Person", "mail", "email", "failed")]


async def test_a_job_that_fails_finishing_marks_the_rename_failed(job: dict[str, Any]) -> None:
    job["finish_error"] = HTTPException(status_code=409, detail="busy")
    with pytest.raises(HTTPException):
        await schema_rename.run_property_job(_Pool(_Conn()), "acme",  # type: ignore[arg-type]
                                             {"on": "Person", "from": "mail", "to": "email"})
    assert job["marked"][0][-1] == "failed"


async def test_finishing_under_a_busy_schema_lock_answers_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextlib.asynccontextmanager
    async def busy(_pool: object, tenant: str, _if_match: object) -> AsyncIterator[None]:
        raise schema_version.SchemaBusyError(tenant, "a schema write holds the tenant")
        yield  # pragma: no cover

    monkeypatch.setattr(schema_version, "write", busy)
    with pytest.raises(HTTPException) as refused:
        await schema_rename.finish_property(_Pool(_Conn()), "acme", "Person", "mail", "email",  # type: ignore[arg-type]
                                            ["Person"])
    assert refused.value.status_code == 409  # noqa: PLR2004
    assert "try again when it ends" in str(refused.value.detail)


async def test_finishing_writes_the_renamed_spec_and_drops_the_old_indexes(
    monkeypatch: pytest.MonkeyPatch, quiet: dict[str, list[Any]],
) -> None:
    @contextlib.asynccontextmanager
    async def free(_pool: object, _tenant: str, _if_match: object) -> AsyncIterator[None]:
        yield None

    conn = _Conn()

    async def load_spec(_pool: object, _tenant: str) -> TenantSpec:
        # The spec as stored: the renamed one once the rename wrote it.
        return SPEC if conn.stored is None else spec_from_json(json.dumps(conn.stored))

    async def no_more(_pool: object, **_: Any) -> int:
        return 0

    async def built(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(schema_version, "write", free)
    monkeypatch.setattr(schema_rename, "load_spec", load_spec)
    monkeypatch.setattr(writes, "rename_property_batch", no_more)
    monkeypatch.setattr(schema_rename, "bootstrap_tenant", built)
    monkeypatch.setattr(schema_rename, "ensure_search_indexes", built)
    await schema_rename.finish_property(_Pool(conn), "acme", "Person", "mail", "email",  # type: ignore[arg-type]
                                        ["Person", "Employee"])
    assert conn.stored is not None
    assert conn.stored["property_indexes"][0] == {"label": "Person", "properties": ["email"]}
    assert conn.sql("DELETE FROM graphos_meta.node_properties")
    assert quiet["changes"][0]["detail"]["renamed"] == {
        "kind": "property", "on": "Person", "from": "mail", "to": "email",
    }
    old_index = declared.index_name({"label": "Person", "properties": ["mail"]})
    assert conn.sql(f'DROP INDEX IF EXISTS "acme"."{old_index}"')
    kept = declared.index_name({"label": "Doc", "properties": ["kind"]})
    assert not conn.sql(kept)


# ── Dropping a deprecated label (the transaction around check_drop) ────────


async def test_a_deprecated_empty_label_is_dropped_with_everything_naming_it(
    quiet: dict[str, list[Any]],
) -> None:
    spec = replace(SPEC, deprecated={"vertex": {"Team": {"deprecated": True}}},
                   resolve_rules=())
    conn = _Conn(spec, rows=0, saved=[("teams", "MATCH (t:Team) RETURN t")],
                 pairs=[("MEMBER_OF", "Person", "Team")])
    saved = await schema_rename.drop_label(_Pool(conn), "acme", "Team", by="ops")  # type: ignore[arg-type]
    assert saved == ["teams"]
    assert conn.sql("ag_catalog.drop_label")
    assert conn.stored is not None and "Team" not in conn.stored["vertex_labels"]  # noqa: PT018
    assert quiet["removals"] == [("acme", "vertex_label", "Team", {"by": "ops"})]
    assert quiet["changes"][0]["detail"] == {"dropped": {"kind": "label", "name": "Team"}}


async def test_a_label_with_rows_is_not_dropped(quiet: dict[str, list[Any]]) -> None:
    spec = replace(SPEC, deprecated={"vertex": {"Doc": {"deprecated": True}}})
    conn = _Conn(spec, rows=4)
    with pytest.raises(RenameError, match="'Doc' has 4 row\\(s\\): delete them first"):
        await schema_rename.drop_label(_Pool(conn), "acme", "Doc")  # type: ignore[arg-type]
    assert not conn.sql("drop_label")


async def test_a_deprecated_edge_type_is_dropped_with_its_pairs(
    quiet: dict[str, list[Any]],
) -> None:
    spec = replace(SPEC, deprecated={"edge": {"WROTE": {"deprecated": True}}})
    conn = _Conn(spec, rows=0, pairs=[("WROTE", "Person", "Doc")])
    await schema_rename.drop_label(_Pool(conn), "acme", "WROTE")  # type: ignore[arg-type]
    assert conn.sql("DELETE FROM graphos_meta.edge_endpoints WHERE tenant = $1 AND edge_type")
    assert quiet["removals"][0][1] == "edge_type"


async def test_dropping_a_label_in_use_answers_busy(quiet: dict[str, list[Any]]) -> None:
    conn = _Conn(lock_error=asyncpg.LockNotAvailableError("lock timeout"))
    with pytest.raises(TenantBusyError, match="is in use"):
        await schema_rename.drop_label(_Pool(conn), "acme", "Team")  # type: ignore[arg-type]

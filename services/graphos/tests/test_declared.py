"""Declared property indexes and generated columns: the wire contract, the DDL
graphos writes for them, and the conversion of statements stored before 0.6.0.

No SQL reaches graphos from a client any more, so the contract checks are on
names: anything that is not a plain identifier is refused before any DDL is
written.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from graphos import declared
from graphos.contracts.tenants import PropertyIndexSpec, TenantSpecBody

_BASE: dict[str, Any] = {"name": "demo", "vertex_labels": ["Doc"], "edge_labels": ["LINKS"]}


# ── the contract ───────────────────────────────────────────────────────────


def test_sql_is_refused_with_a_pointer_to_the_declarations() -> None:
    with pytest.raises(ValidationError, match="property_indexes"):
        TenantSpecBody(**_BASE, indexes=['CREATE INDEX IF NOT EXISTS x ON demo."Doc" (id)'])
    with pytest.raises(ValidationError, match="no longer accepted"):
        TenantSpecBody(**_BASE, generated_columns=[
            'ALTER TABLE demo."Doc" ADD COLUMN c text GENERATED ALWAYS AS (1) STORED',
        ])


def test_empty_lists_from_older_clients_are_accepted() -> None:
    # Clients before 0.6.0 send both fields on every registration, empty
    # unless they used SQL: no SQL, nothing to refuse.
    body = TenantSpecBody(**_BASE, indexes=[], generated_columns=[])
    assert body.property_indexes == [] and body.generated_columns == []
    assert "indexes" not in body.model_dump()


def test_declarations_are_accepted() -> None:
    body = TenantSpecBody(**_BASE, property_indexes=[
        {"label": "Doc", "properties": ["kind"]},
        {"label": "Doc", "properties": ["owner", "kind"], "unique": True, "name": "doc_owner"},
        {"label": "Doc", "properties": ["title"], "kind": "trigram"},
    ], generated_columns=[
        {"label": "Doc", "column": "kind", "property": "kind"},
        {"label": "Doc", "column": "score", "property": "score", "type": "numeric"},
    ])
    assert [x.kind for x in body.property_indexes] == ["equality", "equality", "trigram"]
    assert body.generated_columns[0].type == "text"


@pytest.mark.parametrize(
    "idx",
    [
        {"label": "Doc", "properties": ["a'); DROP TABLE x; --"]},
        {"label": 'Doc"; DROP', "properties": ["kind"]},
        {"label": "Doc", "properties": ["kind"], "name": "x; DROP TABLE y"},
        {"label": "Doc", "properties": []},
        {"label": "Doc", "properties": ["a", "b", "c", "d", "e"]},
        {"label": "Doc", "properties": ["a", "a"]},
        {"label": "Doc", "properties": ["a", "b"], "kind": "trigram"},
        {"label": "Doc", "properties": ["a"], "kind": "trigram", "unique": True},
        {"label": "Doc", "properties": ["a"], "kind": "gist"},
        {"label": "Doc", "properties": ["a"], "where": "true"},          # no extra keys
    ],
)
def test_bad_property_indexes_are_refused(idx: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TenantSpecBody(**_BASE, property_indexes=[idx])


@pytest.mark.parametrize(
    "col",
    [
        {"label": "Doc", "column": "graphos_rev", "property": "x"},
        {"label": "Doc", "column": "end_community_id", "property": "x"},
        {"label": "Doc", "column": "community_id", "property": "team"},  # graphos's own name
        {"label": "Doc", "column": "properties", "property": "x"},
        {"label": "Doc", "column": "Kind", "property": "x"},              # lower case only
        {"label": "Doc", "column": "k", "property": "x'||pg_sleep(1)||'"},
        {"label": "Doc", "column": "k", "property": "x", "type": "bigint"},
        {"label": "Doc", "column": "k", "property": "x", "expression": "1"},
    ],
)
def test_bad_generated_columns_are_refused(col: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TenantSpecBody(**_BASE, generated_columns=[col])


def test_a_column_declared_twice_must_agree() -> None:
    col = {"label": "Doc", "column": "k", "property": "kind"}
    assert len(TenantSpecBody(**_BASE, generated_columns=[col, col]).generated_columns) == 2
    with pytest.raises(ValidationError, match="declared twice"):
        TenantSpecBody(**_BASE, generated_columns=[col, {**col, "type": "jsonb"}])


# ── the DDL graphos writes ─────────────────────────────────────────────────


def _idx(**fields: Any) -> dict[str, Any]:
    return PropertyIndexSpec(label="Doc", **fields).model_dump()


def test_an_index_uses_the_expression_the_where_filters_use() -> None:
    assert declared.index_definition(_idx(properties=["kind", "owner"])) == (
        "USING btree (((((properties)::text)::jsonb ->> 'kind')), "
        "((((properties)::text)::jsonb ->> 'owner')))"
    )
    assert declared.index_definition(_idx(properties=["title"], kind="trigram")) == (
        "USING gin (((((properties)::text)::jsonb ->> 'title')) gin_trgm_ops)"
    )


def test_index_names() -> None:
    assert declared.index_name(_idx(properties=["kind"])) == "Doc_kind_btree_idx"
    assert declared.index_name(_idx(properties=["a", "b"], unique=True)) == "Doc_a_b_unique_idx"
    assert declared.index_name(_idx(properties=["t"], kind="trigram")) == "Doc_t_trgm_idx"
    assert declared.index_name(_idx(properties=["kind"], name="mine")) == "mine"
    long = declared.index_name(_idx(properties=["p" * 60]))
    assert len(long) <= 63 and long.endswith("_idx")  # noqa: PLR2004


def test_column_expressions_never_cast_a_value_of_another_type() -> None:
    col = {"label": "Doc", "column": "c", "property": "n"}
    assert declared.column_expression({**col, "type": "text"}) == (
        "(((properties)::text)::jsonb ->> 'n')"
    )
    assert declared.column_expression({**col, "type": "numeric"}) == (
        "CASE WHEN jsonb_typeof(((properties)::text)::jsonb -> 'n') = 'number' "
        "THEN ((((properties)::text)::jsonb ->> 'n'))::numeric END"
    )
    assert "= 'boolean'" in declared.column_expression({**col, "type": "boolean"})
    assert declared.column_definition({**col, "type": "jsonb"}) == (
        "jsonb GENERATED ALWAYS AS ((((properties)::text)::jsonb -> 'n')) STORED"
    )


# ── statements stored before 0.6.0 ─────────────────────────────────────────


def _convert(
    indexes: list[str], columns: list[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    return declared.convert_legacy({
        "name": "acme", "vertex_labels": ["Document", "User"], "edge_labels": ["AUTHORED"],
        "indexes": indexes, "generated_columns": columns or [],
    })


def test_statements_that_say_what_a_declaration_says_are_converted() -> None:
    spec, dropped = _convert([
        'CREATE INDEX IF NOT EXISTS doc_kind ON "acme"."Document" '
        "(((properties::text)::jsonb ->> 'kind'))",
        "create unique index if not exists user_email on acme.\"User\" "
        "((((properties)::text)::jsonb ->> 'email'), ((properties::text::jsonb ->> 'org')))",
        'CREATE INDEX IF NOT EXISTS doc_title ON "acme"."Document" USING gin '
        "(((properties::text)::jsonb ->> 'title') gin_trgm_ops);",
    ], [
        'ALTER TABLE "acme"."Document" ADD COLUMN IF NOT EXISTS kind text '
        "GENERATED ALWAYS AS (((properties::text)::jsonb ->> 'kind')) STORED",
        'ALTER TABLE "acme"."Document" ADD COLUMN meta jsonb '
        "GENERATED ALWAYS AS ((properties::text)::jsonb -> 'meta') STORED",
    ])
    assert dropped == []
    assert "indexes" not in spec
    assert spec["property_indexes"] == [
        {"label": "Document", "properties": ["kind"], "kind": "equality", "unique": False,
         "name": "doc_kind"},
        {"label": "User", "properties": ["email", "org"], "kind": "equality", "unique": True,
         "name": "user_email"},
        {"label": "Document", "properties": ["title"], "kind": "trigram", "unique": False,
         "name": "doc_title"},
    ]
    assert spec["generated_columns"] == [
        {"label": "Document", "column": "kind", "property": "kind", "type": "text"},
        {"label": "Document", "column": "meta", "property": "meta", "type": "jsonb"},
    ]
    # Each converted declaration is one the contract accepts.
    TenantSpecBody(name="acme", vertex_labels=["Document", "User"], **{
        k: spec[k] for k in ("property_indexes", "generated_columns")
    })


@pytest.mark.parametrize(
    "stmt",
    [
        # Partial and covering indexes, other expressions and methods.
        'CREATE INDEX IF NOT EXISTS a ON "acme"."Document" '
        "(((properties::text)::jsonb ->> 'kind')) WHERE true",
        'CREATE INDEX IF NOT EXISTS b ON "acme"."Document" '
        "(((properties::text)::jsonb ->> 'kind')) INCLUDE (id)",
        "CREATE INDEX IF NOT EXISTS c ON \"acme\".\"Document\" ((properties -> '\"kind\"'))",
        "CREATE INDEX IF NOT EXISTS d ON \"acme\".\"Document\" "
        "(lower((properties::text)::jsonb ->> 'name'))",
        'CREATE INDEX IF NOT EXISTS e ON "acme"."Document" USING hash '
        "(((properties::text)::jsonb ->> 'kind'))",
        # An edge label, and AGE's parent table.
        'CREATE INDEX IF NOT EXISTS f ON "acme"."AUTHORED" (end_id)',
        'CREATE INDEX IF NOT EXISTS g ON "acme"."_ag_label_vertex" (id)',
    ],
)
def test_other_index_statements_are_left_out(stmt: str) -> None:
    spec, dropped = _convert([stmt])
    assert dropped == [stmt]
    assert spec["property_indexes"] == []


def test_other_column_statements_are_left_out() -> None:
    stmts = [
        'ALTER TABLE "acme"."Document" ADD COLUMN score double precision '
        "GENERATED ALWAYS AS (((properties::text)::jsonb ->> 's')::float8) STORED",
        'ALTER TABLE "acme"."Document" ADD COLUMN "Kind" text '
        "GENERATED ALWAYS AS (((properties::text)::jsonb ->> 'kind')) STORED",
    ]
    spec, dropped = _convert([], stmts)
    assert dropped == stmts
    assert spec["generated_columns"] == []


def test_a_converted_spec_is_no_longer_legacy() -> None:
    spec, _ = _convert(['CREATE INDEX IF NOT EXISTS x ON "acme"."Document" (id)'])
    assert declared.has_legacy({"indexes": []})
    assert not declared.has_legacy(spec)
    assert declared.convert_legacy(spec) == (spec, [])

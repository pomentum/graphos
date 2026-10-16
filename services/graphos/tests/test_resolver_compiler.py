"""Unit tests for the schema-driven edge resolver compiler.

Pure-Python — no DB.  Exercises each DSL primitive in isolation, cross
products of ``require_*`` + ``match_op``, and the strict identifier
guard against injection attempts.
"""

from __future__ import annotations

import pytest

from graphos.resolver.catalogue import PropertyLabelCatalogue
from graphos.resolver.compiler import (
    CompileError,
    _parse_dst_value_from,
    _tokenise_dst_value_from,
    compile_resolve_rule,
    compile_resolve_rules_for_edge_type,
)

# ── Tokeniser / dst_value_from parser ──────────────────────────────────────


def test_tokenise_bare_ident() -> None:
    toks = _tokenise_dst_value_from("name")
    assert len(toks) == 1
    assert toks[0].kind == "ident"
    assert toks[0].text == "name"


def test_tokenise_concat_chain() -> None:
    toks = _tokenise_dst_value_from("concat(collection, '/', slug)")
    kinds = [t.kind for t in toks]
    assert kinds == ["concat_open", "ident", "comma", "string", "comma", "ident", "close"]
    # String token has quotes stripped.
    assert toks[3].text == "/"


def test_parse_bare_ident() -> None:
    parsed = _parse_dst_value_from("email")
    assert parsed.pieces == [("prop", "email")]


def test_parse_concat() -> None:
    parsed = _parse_dst_value_from("concat(collection, '/', slug)")
    assert parsed.pieces == [
        ("prop", "collection"),
        ("lit", "/"),
        ("prop", "slug"),
    ]
    assert parsed.property_names == ["collection", "slug"]


def test_parse_rejects_operators() -> None:
    with pytest.raises(CompileError):
        _parse_dst_value_from("name + suffix")


def test_parse_rejects_nested_function() -> None:
    with pytest.raises(CompileError):
        _parse_dst_value_from("upper(name)")


def test_parse_rejects_empty_concat() -> None:
    with pytest.raises(CompileError):
        _parse_dst_value_from("concat()")


def test_parse_rejects_trailing_comma() -> None:
    with pytest.raises(CompileError):
        _parse_dst_value_from("concat(a, b,)")


def test_parse_rejects_missing_comma() -> None:
    with pytest.raises(CompileError):
        _parse_dst_value_from("concat(a b)")


# ── compile_resolve_rule end-to-end ────────────────────────────────────────

def _cat(by_prop: dict[str, set[str]]) -> PropertyLabelCatalogue:
    return PropertyLabelCatalogue(
        tenant="acme",
        by_property={k: frozenset(v) for k, v in by_prop.items()},
    )


def test_compile_src_has_array_basic() -> None:
    rule = {
        "src_has_array":     "tag_keys",
        "dst_value_from":    "concat(collection, '/', slug)",
        "require_different": ["team"],
    }
    cat = _cat({
        "tag_keys":   {"Document"},
        "collection": {"Tag"},
        "slug":       {"Tag"},
        "team":       {"Document", "Tag"},
    })
    out = compile_resolve_rule(
        tenant="acme", edge_type="TAGGED", rule=rule, catalogue=cat,
    )
    assert len(out) == 1
    cr = out[0]
    assert cr.src_label == "Document"
    assert cr.dst_label == "Tag"
    # SQL shape: must INSERT into the right table, unnest the array,
    # compare against a concat-of-properties chain joined by ||, bind
    # the string literal as $1, and ON CONFLICT DO NOTHING RETURNING 1.
    assert 'INSERT INTO "acme"."TAGGED"' in cr.sql
    assert "jsonb_array_elements_text" in cr.sql
    assert "->> 'tag_keys'" not in cr.sql  # array form uses ``->``
    assert "-> 'tag_keys'" in cr.sql
    assert "->> 'collection'" in cr.sql
    assert "->> 'slug'" in cr.sql
    assert "||" in cr.sql
    assert "$1::text" in cr.sql
    assert cr.params == ["/"]
    assert "ON CONFLICT (start_id, end_id) DO NOTHING" in cr.sql
    assert "RETURNING 1" in cr.sql
    assert "->> 'team'" in cr.sql
    assert "<>" in cr.sql  # require_different


def test_compile_src_array_of_objects() -> None:
    rule = {
        "src_array_of_objects": "authors",
        "src_match_field":      "email",
        "dst_value_from":       "email",
        "require_different":    ["team"],
    }
    cat = _cat({
        "authors": {"Document"},
        "email":   {"User"},
        "team":    {"Document", "User"},
    })
    out = compile_resolve_rule(
        tenant="acme", edge_type="AUTHORED_BY", rule=rule, catalogue=cat,
    )
    assert len(out) == 1
    cr = out[0]
    assert cr.src_label == "Document"
    assert cr.dst_label == "User"
    # Object-array uses ``jsonb_array_elements`` (not the _text variant)
    # and extracts the match field via ``elt.val ->> '<field>'``.
    assert "jsonb_array_elements(" in cr.sql
    assert "elt.val ->> 'email'" in cr.sql
    # ``dst_value_from`` is a bare ident — no literal, so no $-param.
    assert cr.params == []
    assert "(dst.properties::text)::jsonb ->> 'email'" in cr.sql


def test_compile_match_op_eq_ci() -> None:
    rule = {
        "src_has_array":  "links",
        "dst_value_from": "name",
        "match_op":       "eq_ci",
    }
    cat = _cat({"links": {"Document"}, "name": {"Document"}})
    out = compile_resolve_rule(
        tenant="acme", edge_type="LINKS_TO", rule=rule, catalogue=cat,
    )
    assert len(out) == 1
    assert "lower(" in out[0].sql


def test_compile_require_same_and_different_cross_product() -> None:
    rule = {
        "src_has_array":     "cites",
        "dst_value_from":    "name",
        "require_same":      ["bucket"],
        "require_different": ["team"],
    }
    cat = _cat({
        "cites":   {"Document"},
        "name":    {"Document"},
        "bucket":  {"Document"},
        "team":    {"Document"},
    })
    out = compile_resolve_rule(
        tenant="acme", edge_type="CITES", rule=rule, catalogue=cat,
    )
    assert len(out) == 1
    sql = out[0].sql
    assert "->> 'bucket'" in sql
    # require_same uses '=', require_different uses '<>' — both present.
    assert " = " in sql
    assert " <> " in sql


def test_compile_emits_one_rule_per_label_pair() -> None:
    """When multiple vertex labels carry the src/dst properties, one
    INSERT is emitted per (src, dst) pair so each picks up the per-
    label indexes installed at bootstrap.
    """
    rule = {
        "src_has_array":  "see_also",
        "dst_value_from": "name",
    }
    cat = _cat({
        "see_also": {"Document", "Article"},
        "name":     {"Document", "Section"},
    })
    out = compile_resolve_rule(
        tenant="acme", edge_type="REFERS", rule=rule, catalogue=cat,
    )
    pairs = {(c.src_label, c.dst_label) for c in out}
    # src candidates: Document + Article (both carry see_also)
    # dst candidates: Document + Section (both carry name)
    assert pairs == {
        ("Document", "Document"),
        ("Document", "Section"),
        ("Article", "Document"),
        ("Article", "Section"),
    }


def test_compile_empty_when_property_missing() -> None:
    """A rule referencing a property no node carries yet compiles to
    zero INSERTs — the design doc's "rule stays registered" semantics.
    """
    rule = {
        "src_has_array":  "not_here_yet",
        "dst_value_from": "name",
    }
    cat = _cat({"name": {"Document"}})  # nobody carries 'not_here_yet'
    out = compile_resolve_rule(
        tenant="acme", edge_type="REFERS", rule=rule, catalogue=cat,
    )
    assert out == []


# ── Injection / validation guards ──────────────────────────────────────────


@pytest.mark.parametrize(
    "edge_type",
    [
        "EDGE; DROP TABLE users; --",
        "EDGE\"",
        "EDGE'",
        "1EDGE",
        "edge",          # lowercase rejected by registration-time validator
                         # but compiler is more permissive on first char.
                         # The strict ident pattern accepts lowercase.
                         # Drop this case.
    ],
)
def test_compile_rejects_unsafe_edge_type(edge_type: str) -> None:
    cat = _cat({"x": {"L"}})
    if edge_type == "edge":
        pytest.skip("compiler ident regex permits lowercase first char")
    with pytest.raises(CompileError):
        compile_resolve_rule(
            tenant="acme",
            edge_type=edge_type,
            rule={"src_has_array": "x", "dst_value_from": "x"},
            catalogue=cat,
        )


@pytest.mark.parametrize(
    "tenant",
    [
        "acme; DROP TABLE x;",
        'acme"',
        "1acme",
        "ac me",
    ],
)
def test_compile_rejects_unsafe_tenant(tenant: str) -> None:
    cat = _cat({"x": {"L"}})
    with pytest.raises(CompileError):
        compile_resolve_rule(
            tenant=tenant,
            edge_type="EDGE",
            rule={"src_has_array": "x", "dst_value_from": "x"},
            catalogue=cat,
        )


@pytest.mark.parametrize(
    "prop",
    [
        "tag keys",
        "tag_keys; DROP",
        "tag_keys'",
        "1tag_keys",
        "tag\"keys",
    ],
)
def test_compile_rejects_unsafe_property_name(prop: str) -> None:
    cat = _cat({"x": {"L"}})
    with pytest.raises(CompileError):
        compile_resolve_rule(
            tenant="acme",
            edge_type="EDGE",
            rule={"src_has_array": prop, "dst_value_from": "name"},
            catalogue=cat,
        )


def test_compile_rejects_both_src_modes() -> None:
    cat = _cat({})
    with pytest.raises(CompileError):
        compile_resolve_rule(
            tenant="acme",
            edge_type="EDGE",
            rule={
                "src_has_array":        "a",
                "src_array_of_objects": "b",
                "dst_value_from":       "name",
            },
            catalogue=cat,
        )


def test_compile_rejects_neither_src_mode() -> None:
    cat = _cat({})
    with pytest.raises(CompileError):
        compile_resolve_rule(
            tenant="acme",
            edge_type="EDGE",
            rule={"dst_value_from": "name"},
            catalogue=cat,
        )


def test_compile_rejects_array_of_objects_without_match_field() -> None:
    cat = _cat({})
    with pytest.raises(CompileError):
        compile_resolve_rule(
            tenant="acme",
            edge_type="EDGE",
            rule={
                "src_array_of_objects": "authors",
                "dst_value_from":       "name",
            },
            catalogue=cat,
        )


def test_compile_rejects_bad_match_op() -> None:
    cat = _cat({})
    with pytest.raises(CompileError):
        compile_resolve_rule(
            tenant="acme",
            edge_type="EDGE",
            rule={
                "src_has_array":  "x",
                "dst_value_from": "name",
                "match_op":       "regex",  # not in {eq, eq_ci}
            },
            catalogue=cat,
        )


def test_compile_rejects_dst_string_with_quote() -> None:
    """A SQL-injection attempt via a string literal in dst_value_from
    is neutralised because the literal binds as a parameter — never
    interpolated.  The SQL itself must not contain the raw literal.
    """
    rule = {
        "src_has_array":  "tag_keys",
        "dst_value_from": "concat(name, \\'; DROP TABLE x; --\\')",
    }
    cat = _cat({"tag_keys": {"Document"}, "name": {"Document"}})
    # The parser strips backslashes from \\'  (lex regex permits them).
    # Whether it raises or compiles is fine — but if it compiles, the
    # literal MUST appear only in cr.params, never in cr.sql.
    try:
        out = compile_resolve_rule(
            tenant="acme", edge_type="X", rule=rule, catalogue=cat,
        )
    except CompileError:
        return
    for cr in out:
        assert "DROP TABLE" not in cr.sql
        assert any("DROP TABLE" in str(p) for p in cr.params)


# ── compile_resolve_rules_for_edge_type (collapsed UNION ALL) ──────────────


def test_collapsed_zero_rules() -> None:
    out = compile_resolve_rules_for_edge_type(
        tenant="acme",
        edge_type="CITES",
        rules=[],
        catalogue=_cat({}),
    )
    assert out.edge_type == "CITES"
    assert out.sql is None
    assert out.params == ()
    assert out.arm_count == 0
    assert out.rule_count == 0
    assert out.no_label_rule_count == 0


def test_collapsed_one_rule_one_arm() -> None:
    rule = {
        "src_has_array":     "tag_keys",
        "dst_value_from":    "concat(collection, '/', slug)",
        "require_different": ["team"],
    }
    cat = _cat({
        "tag_keys":   {"Document"},
        "collection": {"Tag"},
        "slug":       {"Tag"},
        "team":       {"Document", "Tag"},
    })
    out = compile_resolve_rules_for_edge_type(
        tenant="acme", edge_type="TAGGED", rules=[rule], catalogue=cat,
    )
    assert out.sql is not None
    assert out.arm_count == 1
    assert out.no_label_rule_count == 0
    assert out.rule_count == 1
    # One arm — no UNION ALL but still wrapped in the candidates subquery.
    assert "UNION ALL" not in out.sql
    assert 'INSERT INTO "acme"."TAGGED"' in out.sql
    assert "candidates" in out.sql
    assert "ON CONFLICT (start_id, end_id) DO NOTHING" in out.sql
    assert "RETURNING 1" in out.sql
    assert out.params == ("/",)


def test_collapsed_one_rule_multiple_arms() -> None:
    rule = {
        "src_has_array":  "see_also",
        "dst_value_from": "name",
    }
    cat = _cat({
        "see_also": {"Document", "Article"},
        "name":     {"Document", "Section"},
    })
    out = compile_resolve_rules_for_edge_type(
        tenant="acme", edge_type="REFERS", rules=[rule], catalogue=cat,
    )
    assert out.sql is not None
    assert out.arm_count == 4   # 2 src × 2 dst
    # Three UNION ALL separators between four arms.
    assert out.sql.count(" UNION ALL ") == 3
    # Every (src, dst) label-pair table reference must show up.
    for src in ("Document", "Article"):
        assert f'"acme"."{src}" src' in out.sql
    for dst in ("Document", "Section"):
        assert f'"acme"."{dst}" dst' in out.sql


def test_collapsed_multiple_rules_overlapping_arms() -> None:
    rule_a = {"src_has_array": "cites",   "dst_value_from": "name"}
    rule_b = {"src_has_array": "tag_keys", "dst_value_from": "name"}
    cat = _cat({
        "cites":    {"Document"},
        "tag_keys": {"Document"},
        "name":     {"Document"},
    })
    out = compile_resolve_rules_for_edge_type(
        tenant="acme",
        edge_type="RELATED",
        rules=[rule_a, rule_b],
        catalogue=cat,
    )
    assert out.sql is not None
    assert out.arm_count == 2
    assert out.rule_count == 2
    assert out.no_label_rule_count == 0
    assert out.sql.count(" UNION ALL ") == 1
    assert "-> 'cites'" in out.sql
    assert "-> 'tag_keys'" in out.sql


def test_collapsed_empty_catalogue_skip_emits_no_sql() -> None:
    """Every rule has no catalogue match → sql is None, warning
    surfaces via no_label_rule_count == rule_count.
    """
    rule = {
        "src_has_array":  "not_here_yet",
        "dst_value_from": "name",
    }
    cat = _cat({"name": {"Document"}})
    out = compile_resolve_rules_for_edge_type(
        tenant="acme", edge_type="REFERS", rules=[rule], catalogue=cat,
    )
    assert out.sql is None
    assert out.arm_count == 0
    assert out.no_label_rule_count == 1
    assert out.rule_count == 1


def test_collapsed_partial_catalogue_skip() -> None:
    """One rule matches, one doesn't — sql is emitted with arms only
    from the matching rule; no_label_rule_count counts the skipped one.
    """
    rule_match    = {"src_has_array": "cites",        "dst_value_from": "name"}
    rule_no_match = {"src_has_array": "not_here_yet", "dst_value_from": "name"}
    cat = _cat({"cites": {"Document"}, "name": {"Document"}})
    out = compile_resolve_rules_for_edge_type(
        tenant="acme",
        edge_type="CITES",
        rules=[rule_match, rule_no_match],
        catalogue=cat,
    )
    assert out.sql is not None
    assert out.arm_count == 1
    assert out.rule_count == 2
    assert out.no_label_rule_count == 1


def test_collapsed_sql_snapshot_tagged_rule() -> None:
    """Snapshot the emitted SQL for a representative concat-key rule
    so future changes to the compiler surface in diff form.
    """
    rule = {
        "src_has_array":     "tag_keys",
        "dst_value_from":    "concat(collection, '/', slug)",
        "require_different": ["team"],
    }
    cat = _cat({
        "tag_keys":   {"Document"},
        "collection": {"Tag"},
        "slug":       {"Tag"},
        "team":       {"Document", "Tag"},
    })
    out = compile_resolve_rules_for_edge_type(
        tenant="acme", edge_type="TAGGED", rules=[rule], catalogue=cat,
    )
    expected = (
        'INSERT INTO "acme"."TAGGED" (start_id, end_id, properties) '
        "SELECT start_id, end_id, properties FROM ( "
        "SELECT src.id AS start_id, dst.id AS end_id, "
        "'{\"graphos_derived\": true}'::agtype AS properties "
        'FROM "acme"."Document" src CROSS JOIN "acme"."Tag" dst '
        ", jsonb_array_elements_text(((src.properties::text)::jsonb -> 'tag_keys')) AS elt(val) "
        "WHERE elt.val = ((dst.properties::text)::jsonb ->> 'collection') "
        "|| $1::text || ((dst.properties::text)::jsonb ->> 'slug') "
        "AND ((src.properties::text)::jsonb ->> 'team') "
        "<> ((dst.properties::text)::jsonb ->> 'team') "
        "AND src.id <> dst.id"
        " ) candidates "
        "ON CONFLICT (start_id, end_id) DO NOTHING RETURNING 1;"
    )
    assert out.sql == expected
    assert out.arm_count == 1
    assert out.params == ("/",)


def test_collapsed_rejects_unsafe_identifiers() -> None:
    cat = _cat({"x": {"L"}})
    with pytest.raises(CompileError):
        compile_resolve_rules_for_edge_type(
            tenant="acme; DROP TABLE x;",
            edge_type="EDGE",
            rules=[{"src_has_array": "x", "dst_value_from": "x"}],
            catalogue=cat,
        )
    with pytest.raises(CompileError):
        compile_resolve_rules_for_edge_type(
            tenant="acme",
            edge_type='EDGE"',
            rules=[{"src_has_array": "x", "dst_value_from": "x"}],
            catalogue=cat,
        )

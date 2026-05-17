"""Unit tests for the agtype parser and the cypher-literal serialiser."""

from __future__ import annotations

import pytest

from graphos.cypher import cypher_map_literal, cypher_value_literal, parse_agtype


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # passthrough non-strings
        (None, None),
        (42, 42),
        # bare scalars
        ('"foo"', "foo"),
        ("123", 123),
        ("true", True),
        ("false", False),
        ("null", None),
        # annotated scalars
        ("12345::numeric", 12345),
        ('"x"::string', "x"),
        # collections
        ("[1, 2, 3]", [1, 2, 3]),
        ('{"name": "foo"}', {"name": "foo"}),
        # annotated map (vertex / edge shape)
        ('{"id": 1, "name": "x"}::vertex', {"id": 1, "name": "x"}),
    ],
)
def test_parse_agtype(raw, expected) -> None:
    assert parse_agtype(raw) == expected


def test_parse_agtype_bare_identifier() -> None:
    """Anything that isn't valid JSON falls through verbatim."""
    assert parse_agtype("foo_bar") == "foo_bar"


# ── cypher-literal serialiser ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "null"),
        (True, "true"),
        (False, "false"),
        (42, "42"),
        (3.14, "3.14"),
        ("foo", "'foo'"),
        ("it's", r"'it\'s'"),                                # single-quote escape
        (r"path\to", r"'path\\to'"),                          # backslash escape
        ([1, "x", None], "[1, 'x', null]"),
        ((1, 2), "[1, 2]"),                                   # tuples ok
    ],
)
def test_cypher_value_literal(value, expected) -> None:
    assert cypher_value_literal(value) == expected


def test_cypher_value_literal_nested_map() -> None:
    out = cypher_value_literal({"a": 1, "b": [2, 3]})
    assert out == "{a: 1, b: [2, 3]}"


def test_cypher_value_literal_rejects_unsupported() -> None:
    class Custom:
        pass

    with pytest.raises(TypeError):
        cypher_value_literal(Custom())


def test_cypher_map_literal_plain_keys() -> None:
    out = cypher_map_literal({"name": "foo", "count": 3})
    assert out == "{name: 'foo', count: 3}"


def test_cypher_map_literal_backticked_keys() -> None:
    out = cypher_map_literal({"weird-key": 1, "ok": 2})
    assert out == "{`weird-key`: 1, ok: 2}"


def test_cypher_map_literal_empty() -> None:
    assert cypher_map_literal({}) == "{}"


def test_cypher_map_literal_rejects_backticks_in_keys() -> None:
    with pytest.raises(ValueError):
        cypher_map_literal({"bad`key": 1})

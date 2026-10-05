"""Unit tests for the agtype parser."""

from __future__ import annotations

import pytest

from graphos.cypher import parse_agtype


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

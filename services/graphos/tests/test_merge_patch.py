"""JSON Merge Patch (RFC 7396): the examples of its Appendix A."""

from __future__ import annotations

from typing import Any

import pytest

from graphos.writes import apply_merge_patch

RFC_7396_EXAMPLES: list[tuple[Any, Any, Any]] = [
    ({"a": "b"}, {"a": "c"}, {"a": "c"}),
    ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
    ({"a": "b"}, {"a": None}, {}),
    ({"a": "b", "b": "c"}, {"a": None}, {"b": "c"}),
    ({"a": ["b"]}, {"a": "c"}, {"a": "c"}),
    ({"a": "c"}, {"a": ["b"]}, {"a": ["b"]}),
    ({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}, {"a": {"b": "d"}}),
    ({"a": [{"b": "c"}]}, {"a": [1]}, {"a": [1]}),
    (["a", "b"], ["c", "d"], ["c", "d"]),
    ({"a": "b"}, ["c"], ["c"]),
    ({"a": "foo"}, None, None),
    ({"a": "foo"}, "bar", "bar"),
    ({"e": None}, {"a": 1}, {"e": None, "a": 1}),
    ([1, 2], {"a": "b", "c": None}, {"a": "b"}),
    ({}, {"a": {"bb": {"ccc": None}}}, {"a": {"bb": {}}}),
]


@pytest.mark.parametrize(("target", "patch", "expected"), RFC_7396_EXAMPLES)
def test_rfc_7396_examples(target: Any, patch: Any, expected: Any) -> None:
    assert apply_merge_patch(target, patch) == expected


def test_target_is_not_modified() -> None:
    target = {"a": {"b": 1}}
    apply_merge_patch(target, {"a": {"b": None}})
    assert target == {"a": {"b": 1}}

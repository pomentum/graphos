"""Contract-validation tests for the resolve_rule additive shape on
``POST /v1/tenants``.

Verifies the closed-DSL guarantees from the design doc: only the 7
documented keys, unknown keys reject with a clear message identifying
the bad key and the rule's edge label.
"""

from __future__ import annotations

import pytest

from graphos.contracts.tenants import (
    EdgeLabelSpec,
    ResolveEdgesRequest,
    TenantSpecBody,
)

# ── EdgeLabelSpec direct ───────────────────────────────────────────────────


def test_edge_label_string_backcompat() -> None:
    """A bare string in ``edge_labels`` is promoted to the object form
    with ``resolve_rule=None``, so plain string labels stay valid."""
    body = TenantSpecBody(
        name="acme",
        edge_labels=["FOO", "BAR"],
    )
    assert len(body.edge_labels) == 2
    assert all(isinstance(e, EdgeLabelSpec) for e in body.edge_labels)
    assert body.edge_labels[0].name == "FOO"
    assert body.edge_labels[0].resolve_rule is None


def test_edge_label_with_resolve_rule() -> None:
    body = TenantSpecBody(
        name="acme",
        edge_labels=[
            {
                "name": "TAGGED",
                "resolve_rule": {
                    "src_has_array":     "tag_keys",
                    "dst_value_from":    "concat(collection, '/', slug)",
                    "require_different": ["team"],
                },
            },
        ],
    )
    assert body.edge_labels[0].name == "TAGGED"
    assert body.edge_labels[0].resolve_rule is not None
    assert body.edge_labels[0].resolve_rule["src_has_array"] == "tag_keys"


def test_resolve_rule_unknown_key_rejected_with_clear_message() -> None:
    with pytest.raises(ValueError) as exc:
        TenantSpecBody(
            name="acme",
            edge_labels=[
                {
                    "name": "BAD",
                    "resolve_rule": {
                        "src_has_array":  "x",
                        "dst_value_from": "name",
                        "naughty_key":    "boom",
                    },
                },
            ],
        )
    msg = str(exc.value)
    # The error must name BOTH the offending key AND the edge label
    # the rule belongs to.
    assert "naughty_key" in msg
    assert "BAD" in msg


def test_resolve_rule_rejects_both_src_modes() -> None:
    with pytest.raises(ValueError, match="only one of"):
        TenantSpecBody(
            name="acme",
            edge_labels=[
                {
                    "name": "X",
                    "resolve_rule": {
                        "src_has_array":        "a",
                        "src_array_of_objects": "b",
                        "dst_value_from":       "name",
                    },
                },
            ],
        )


def test_resolve_rule_rejects_neither_src_mode() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        TenantSpecBody(
            name="acme",
            edge_labels=[
                {
                    "name": "X",
                    "resolve_rule": {"dst_value_from": "name"},
                },
            ],
        )


def test_resolve_rule_rejects_array_of_objects_without_match_field() -> None:
    with pytest.raises(ValueError, match="src_match_field"):
        TenantSpecBody(
            name="acme",
            edge_labels=[
                {
                    "name": "X",
                    "resolve_rule": {
                        "src_array_of_objects": "deps",
                        "dst_value_from":       "name",
                    },
                },
            ],
        )


def test_resolve_rule_rejects_match_field_with_scalar_array() -> None:
    with pytest.raises(ValueError, match="applies only"):
        TenantSpecBody(
            name="acme",
            edge_labels=[
                {
                    "name": "X",
                    "resolve_rule": {
                        "src_has_array":   "x",
                        "src_match_field": "email",
                        "dst_value_from":  "name",
                    },
                },
            ],
        )


def test_resolve_rule_rejects_bad_match_op() -> None:
    with pytest.raises(ValueError, match="match_op"):
        TenantSpecBody(
            name="acme",
            edge_labels=[
                {
                    "name": "X",
                    "resolve_rule": {
                        "src_has_array":  "x",
                        "dst_value_from": "name",
                        "match_op":       "regex",
                    },
                },
            ],
        )


def test_resolve_rule_rejects_missing_dst_value_from() -> None:
    with pytest.raises(ValueError, match="dst_value_from"):
        TenantSpecBody(
            name="acme",
            edge_labels=[
                {
                    "name": "X",
                    "resolve_rule": {"src_has_array": "x"},
                },
            ],
        )


@pytest.mark.parametrize("bad", ["1foo", "foo bar", "foo;DROP", "foo'"])
def test_resolve_rule_rejects_unsafe_property_names(bad: str) -> None:
    with pytest.raises(ValueError):
        TenantSpecBody(
            name="acme",
            edge_labels=[
                {
                    "name": "X",
                    "resolve_rule": {
                        "src_has_array":  bad,
                        "dst_value_from": "name",
                    },
                },
            ],
        )


def test_resolve_rule_rejects_unsafe_require_property() -> None:
    with pytest.raises(ValueError):
        TenantSpecBody(
            name="acme",
            edge_labels=[
                {
                    "name": "X",
                    "resolve_rule": {
                        "src_has_array":     "x",
                        "dst_value_from":    "name",
                        "require_different": ["team;DROP"],
                    },
                },
            ],
        )


# ── ResolveEdgesRequest ────────────────────────────────────────────────────


def test_resolve_request_all() -> None:
    body = ResolveEdgesRequest()
    assert body.edge_types is None


def test_resolve_request_with_types() -> None:
    body = ResolveEdgesRequest(edge_types=["REFERENCES", "CITES"])
    assert body.edge_types == ["REFERENCES", "CITES"]


def test_resolve_request_rejects_bad_type() -> None:
    with pytest.raises(ValueError):
        ResolveEdgesRequest(edge_types=["tagged"])  # lowercase
    with pytest.raises(ValueError):
        ResolveEdgesRequest(edge_types=["FOO; DROP"])

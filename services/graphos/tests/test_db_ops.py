"""Unit tests for the db-ops validators.

The endpoint shapes (db_stats, slow_queries, vacuum) are exercised via
their pydantic contracts + the _validated_table_names whitelist. Full
HTTP-level end-to-end against the live stack is straightforward but
DB-dependent; we cover that with manual curl during boot verify.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from graphos.api.db import _validated_table_names


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        ([], []),
        (None, []),
        (["foo"], ['"foo"']),
        (["foo", "bar_baz"], ['"foo"', '"bar_baz"']),
        (["demo.Document"], ['"demo"."Document"']),
        (
            ["demo._ag_label_vertex", "graphos_meta.tenants"],
            ['"demo"."_ag_label_vertex"', '"graphos_meta"."tenants"'],
        ),
    ],
)
def test_validated_table_names_accepts_legitimate(inp, expected) -> None:
    assert _validated_table_names(inp) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "1foo",                     # digit start
        "foo bar",                  # space
        "foo;DROP TABLE",           # injection attempt
        "schema.table.extra",       # over-qualified
        "schema..table",            # empty segment
        ".table",                   # no schema
        "schema.",                  # no table
        "",                         # empty
    ],
)
def test_validated_table_names_rejects(bad: str) -> None:
    with pytest.raises(HTTPException) as ei:
        _validated_table_names([bad])
    assert ei.value.status_code == 422

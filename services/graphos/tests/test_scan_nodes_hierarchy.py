"""POST /nodes:scan over a label hierarchy.

A child label's table inherits its parent's (``label_hierarchy``), so a scan
of the parent reads the child's rows too. Each item must carry the label of
the table its row lives in (the leaf label), not the label requested: a scan
of every label, re-imported, would otherwise write each child node a second
time into the parent.

Rows from the parent and child tables can tie on the sort key (the unique
``id`` index is per table), so paging must break ties by graphid.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest

from graphos import writes
from graphos.api.graph import (
    _cursor_branches,
    _decode_scan_cursor,
    _encode_scan_cursor,
    _normalize_order_by,
    scan_nodes,
)
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.contracts.graph import NodesScanRequest
from graphos.identity import Principal

from .conftest import requires_db

TEST_TENANT = "graphos_test_scan_nodes_hierarchy"
ADMIN = Principal.static("test", admin=True)


def test_cursor_carries_the_graphid_tiebreaker() -> None:
    token = _encode_scan_cursor("Person", "id", ["p1"], 844424930131970)
    assert _decode_scan_cursor(token, ["Person"], "id") == (
        "Person", ["p1"], 844424930131970,
    )


def test_cursor_without_graphid_still_decodes() -> None:
    token = _encode_scan_cursor("Person", "id", ["p1"])
    assert _decode_scan_cursor(token, ["Person"], "id") == ("Person", ["p1"], None)


def test_the_cursor_resumes_with_the_ties_first_and_never_uses_ge() -> None:
    _, keys = _normalize_order_by([{"property": "team"}, {"property": "id", "desc": True}])
    _, node_keys = _normalize_order_by("_node")
    branches = _cursor_branches([*keys, *node_keys], ["a", "p1", 7], next_param_idx=1)
    team, id_ = keys[0]["order_expr"], keys[1]["order_expr"]
    # Deepest first: rows tied on every key, then on the first, then after it.
    assert [frag for frag, _, _ in branches] == [
        f"{team} = ($1::text::agtype) AND {id_} = ($2::text::agtype) "
        "AND v.id > agtype_to_graphid($3::text::agtype)",
        f"{team} = ($1::text::agtype) AND {id_} < ($2::text::agtype)",
        f"{team} > ($1::text::agtype)",
    ]
    assert [params for _, params, _ in branches] == [['"a"', '"p1"', "7"], ['"a"', '"p1"'], ['"a"']]
    assert [idx for _, _, idx in branches] == [4, 3, 2]
    # Apache AGE 1.6 index scans lose the rows equal to the bound of ``>=``.
    assert not any(">=" in frag or "<=" in frag for frag, _, _ in branches)


async def _seed(pool: asyncpg.Pool) -> None:
    await register_tenant(
        pool,
        TenantSpec(
            name=TEST_TENANT,
            vertex_labels=("Person", "Employee"),
            edge_labels=(),
            label_hierarchy=(("Employee", "Person"),),
        ),
    )
    async with pool.acquire() as conn:
        await conn.execute(f'DELETE FROM ONLY "{TEST_TENANT}"."Employee";')
        await conn.execute(f'DELETE FROM ONLY "{TEST_TENANT}"."Person";')
    await writes.upsert_nodes(
        pool, tenant=TEST_TENANT, label="Person",
        rows=[{"id": "p1", "props": {"team": "a"}}, {"id": "x", "props": {"team": "b"}},
              {"id": "z", "props": {"team": "a"}}],
    )
    # "x" exists in both tables: the per-table unique index allows it, and it
    # ties with the Person "x" on the default ``id`` sort.
    await writes.upsert_nodes(
        pool, tenant=TEST_TENANT, label="Employee",
        rows=[{"id": "e1", "props": {"team": "b"}}, {"id": "x", "props": {"team": "a"}}],
    )


async def _scan(
    pool: asyncpg.Pool, labels: list[str], *, limit: int = 500, **extra: Any,
) -> list[tuple[str, str]]:
    """Every ``(id, label)`` of the scan, following the cursor to the end."""
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
    out: list[tuple[str, str]] = []
    cursor: str | None = None
    for _ in range(100):
        body = NodesScanRequest(labels=labels, limit=limit, cursor=cursor, **extra)
        resp = await scan_nodes(TEST_TENANT, body, request, ADMIN)
        out.extend((item.id, item.label) for item in resp.items)
        cursor = resp.next_cursor
        if cursor is None:
            return out
    raise AssertionError("scan did not drain")


PERSONS = [("p1", "Person"), ("x", "Person"), ("z", "Person")]
EMPLOYEES = [("e1", "Employee"), ("x", "Employee")]


@requires_db
@pytest.mark.asyncio
async def test_parent_scan_reports_child_rows_under_their_own_label(
    db_pool: asyncpg.Pool,
) -> None:
    await _seed(db_pool)
    person = await _scan(db_pool, ["Person"])
    assert sorted(person) == sorted(PERSONS + EMPLOYEES)
    assert person.count(("e1", "Employee")) == 1
    assert ("e1", "Person") not in person
    assert await _scan(db_pool, ["Employee"]) == EMPLOYEES


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 500])
@pytest.mark.parametrize(
    "order_by",
    ["id", "_node", [{"property": "id", "desc": True}],
     [{"property": "team"}, {"property": "id"}],                     # ties on both
     [{"property": "team", "desc": True}, {"property": "id"}]],
)
async def test_paging_returns_each_row_once_across_ties(
    db_pool: asyncpg.Pool, limit: int, order_by: Any,
) -> None:
    await _seed(db_pool)
    person = await _scan(db_pool, ["Person"], limit=limit, order_by=order_by)
    assert sorted(person) == sorted(PERSONS + EMPLOYEES)
    assert len(person) == len(set(person))


@requires_db
@pytest.mark.asyncio
async def test_every_label_deduplicated_by_id_and_label_gives_each_node_once(
    db_pool: asyncpg.Pool,
) -> None:
    await _seed(db_pool)
    separately = await _scan(db_pool, ["Person"]) + await _scan(db_pool, ["Employee"])
    together = await _scan(db_pool, ["Person", "Employee"], limit=1)
    for rows in (separately, together):
        assert sorted(set(rows)) == sorted(PERSONS + EMPLOYEES)

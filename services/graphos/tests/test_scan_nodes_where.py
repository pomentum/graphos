"""POST /nodes:scan ``where`` values of every JSON type.

The node scan compares ``properties ->> '<prop>'`` (text, so the per-label
btree indexes apply) with the value's text form. That text form must be the
one Postgres emits for the stored JSON value: ``true`` for a boolean, not
Python's ``True``.
"""

from __future__ import annotations

from types import SimpleNamespace

import asyncpg
import pytest

from graphos import writes
from graphos.api.graph import _compile_scan_where, scan_nodes
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.contracts.graph import NodesScanRequest, NodesScanWhere
from graphos.identity import Principal

from .conftest import requires_db

TEST_TENANT = "graphos_test_scan_nodes_where"
ADMIN = Principal.static("test", admin=True)


def _params(op: str, value: object) -> list:
    clause = NodesScanWhere(property="p", op=op, value=value)
    _, params, _ = _compile_scan_where([clause], next_param_idx=1)
    return params


def test_values_are_compared_in_their_json_text_form() -> None:
    assert _params("=", True) == ["true"]
    assert _params("!=", False) == ["false"]
    assert _params("=", 42) == ["42"]
    assert _params("=", 1.5) == ["1.5"]
    assert _params("=", "True") == ["True"]          # text stays as typed
    assert _params("in", [True, 3, "x"]) == [["true", "3", "x"]]
    assert _params("not in", [False]) == [["false"]]


async def _seed(pool: asyncpg.Pool) -> None:
    await register_tenant(
        pool, TenantSpec(name=TEST_TENANT, vertex_labels=("Function",), edge_labels=()),
    )
    async with pool.acquire() as conn:
        await conn.execute(f'DELETE FROM "{TEST_TENANT}"."Function";')
    await writes.upsert_nodes(
        pool, tenant=TEST_TENANT, label="Function",
        rows=[
            {"id": "a", "props": {"public": True, "lines": 10, "name": "True"}},
            {"id": "b", "props": {"public": False, "lines": 20, "name": "b"}},
            {"id": "c", "props": {"public": True, "lines": 10.5, "name": "c"}},
        ],
    )


async def _ids(pool: asyncpg.Pool, *where: NodesScanWhere) -> list[str]:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
    body = NodesScanRequest(labels=["Function"], where=list(where))
    resp = await scan_nodes(TEST_TENANT, body, request, ADMIN)
    return [item.id for item in resp.items]


@requires_db
@pytest.mark.asyncio
async def test_scan_matches_booleans_numbers_and_text(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    w = NodesScanWhere
    assert await _ids(db_pool, w(property="public", op="=", value=True)) == ["a", "c"]
    assert await _ids(db_pool, w(property="public", op="!=", value=True)) == ["b"]
    assert await _ids(db_pool, w(property="public", op="in", value=[False])) == ["b"]
    assert await _ids(db_pool, w(property="lines", op="=", value=10)) == ["a"]
    assert await _ids(db_pool, w(property="lines", op="=", value=10.5)) == ["c"]
    assert await _ids(db_pool, w(property="name", op="=", value="True")) == ["a"]

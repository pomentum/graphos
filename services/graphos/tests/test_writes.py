"""DB tests for graphos.writes: property merge and deletes.

DB-gated. Which nodes a delete matches is checked against what the Cypher
the endpoints used before (``MATCH (n) WHERE n.<key> = $value``) matches.
"""

from __future__ import annotations

import asyncpg
import pytest

from graphos import writes
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.cypher import CypherError, query_cypher

from .conftest import requires_db

TEST_TENANT = "graphos_test_writes"


async def _seed(pool: asyncpg.Pool) -> None:
    await register_tenant(pool, TenantSpec(
        name=TEST_TENANT,
        vertex_labels=("Document", "Collection"),
        edge_labels=("CITES", "PART_OF"),
    ))
    async with pool.acquire() as conn:
        for t in ("CITES", "PART_OF", "Document", "Collection"):
            await conn.execute(f'DELETE FROM "{TEST_TENANT}"."{t}";')
    await writes.upsert_nodes(pool, tenant=TEST_TENANT, label="Document", rows=[
        {"id": "a", "props": {"team": "red", "n": 1}},
        {"id": "b", "props": {"team": "red"}},
        {"id": "c", "props": {"team": ["red", "blue"]}},   # array: not a match
        {"id": "d", "props": {"team": "blue"}},
        {"id": "e", "props": {"team": 5}},                 # number: not "5"
        {"id": "shared", "props": {"team": "green"}},
    ])
    await writes.upsert_nodes(pool, tenant=TEST_TENANT, label="Collection", rows=[
        {"id": "shared", "props": {"team": "green"}},      # same id, other label
        {"id": "box", "props": {"team": "blue"}},
    ])
    await writes.upsert_edges(
        pool, tenant=TEST_TENANT, src_label="Document", dst_label="Document",
        edge_type="CITES", rows=[
            {"src_id": "a", "dst_id": "b", "props": {}},   # both ends deleted below
            {"src_id": "a", "dst_id": "a", "props": {}},   # self-loop
            {"src_id": "b", "dst_id": "d", "props": {}},
            {"src_id": "d", "dst_id": "c", "props": {}},   # untouched
        ],
    )
    await writes.upsert_edges(
        pool, tenant=TEST_TENANT, src_label="Document", dst_label="Collection",
        edge_type="PART_OF", rows=[
            {"src_id": "d", "dst_id": "box", "props": {}},
            {"src_id": "shared", "dst_id": "shared", "props": {}},
        ],
    )


async def _ids(pool: asyncpg.Pool) -> set[str]:
    rows = await query_cypher(
        pool, graph=TEST_TENANT, cypher="MATCH (n) RETURN n.id AS id",
        return_cols=[("id", "agtype")],
    )
    return {r["id"] for r in rows}


async def _edges(pool: asyncpg.Pool) -> set[tuple[str, str, str]]:
    rows = await query_cypher(
        pool, graph=TEST_TENANT,
        cypher="MATCH (a)-[r]->(b) RETURN a.id AS s, type(r) AS t, b.id AS d",
        return_cols=[("s", "agtype"), ("t", "agtype"), ("d", "agtype")],
    )
    return {(r["s"], r["t"], r["d"]) for r in rows}


@requires_db
@pytest.mark.asyncio
async def test_merge_node_properties(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    label = await writes.merge_node_properties(
        db_pool, tenant=TEST_TENANT, external_id="a", properties={"n": 2, "x": True},
    )
    assert label == "Document"
    rows = await query_cypher(
        db_pool, graph=TEST_TENANT,
        cypher="MATCH (n {id: 'a'}) RETURN properties(n) AS p",
        return_cols=[("p", "agtype")],
    )
    assert rows[0]["p"] == {"id": "a", "team": "red", "n": 2, "x": True}
    assert await writes.merge_node_properties(
        db_pool, tenant=TEST_TENANT, external_id="nope", properties={"x": 1},
    ) is None


@requires_db
@pytest.mark.asyncio
async def test_delete_by_property_matches_cypher_semantics(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    expected = {r["id"] for r in await query_cypher(
        db_pool, graph=TEST_TENANT,
        cypher="MATCH (n) WHERE n.team = $v RETURN n.id AS id",
        return_cols=[("id", "agtype")], params={"v": "red"},
    )}
    assert expected == {"a", "b"}
    before = await _ids(db_pool)

    nodes, edges = await writes.delete_nodes(
        db_pool, tenant=TEST_TENANT, property_key="team", property_value="red",
    )
    # a->b, a->a and b->d go; each edge is counted once.
    assert (nodes, edges) == (2, 3)
    assert await _ids(db_pool) == before - expected
    assert await _edges(db_pool) == {
        ("d", "CITES", "c"), ("d", "PART_OF", "box"), ("shared", "PART_OF", "shared"),
    }
    # Idempotent.
    assert await writes.delete_nodes(
        db_pool, tenant=TEST_TENANT, property_key="team", property_value="red",
    ) == (0, 0)


@requires_db
@pytest.mark.asyncio
async def test_delete_by_property_number_and_array_do_not_match(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    assert await writes.delete_nodes(
        db_pool, tenant=TEST_TENANT, property_key="team", property_value="5",
    ) == (0, 0)
    nodes, _ = await writes.delete_nodes(
        db_pool, tenant=TEST_TENANT, property_key="team", property_value="blue",
    )
    assert nodes == 2                                  # d and box, not c
    assert "c" in await _ids(db_pool)


@requires_db
@pytest.mark.asyncio
async def test_delete_by_id_removes_every_label_and_its_edges(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    nodes, edges = await writes.delete_nodes(
        db_pool, tenant=TEST_TENANT, property_key="id", property_value="shared",
    )
    assert (nodes, edges) == (2, 1)                    # Document + Collection
    assert "shared" not in await _ids(db_pool)
    assert ("shared", "PART_OF", "shared") not in await _edges(db_pool)
    assert len(await _edges(db_pool)) == 5


def test_database_errors_become_cypher_errors_timeouts_do_not() -> None:
    with pytest.raises(CypherError), writes._db_errors("failed", query="q"):
        raise asyncpg.exceptions.UniqueViolationError("duplicate key")
    with pytest.raises(asyncpg.exceptions.QueryCanceledError), writes._db_errors(
        "failed", query="q",
    ):
        raise asyncpg.exceptions.QueryCanceledError("statement timeout")

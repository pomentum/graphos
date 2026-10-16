"""DB tests for the batch node delete: ``writes.delete_nodes_by_ref`` and
``POST /v1/{tenant}/graph/nodes:delete``.

DB-gated.  The tenant has the id ``shared`` in two labels, so a delete by
label and id must take only the node it names.  The API is driven in-process
through httpx's ASGI transport, with the test pool.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import changes, lookup, writes
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_nodes_delete"
QUOTED = 'q"uote\\d'   # an id that naive string quoting into agtype would break


async def _seed(pool: asyncpg.Pool) -> None:
    await register_tenant(pool, TenantSpec(
        name=TENANT, vertex_labels=("Document", "Collection"), edge_labels=("CITES", "PART_OF"),
    ))
    async with pool.acquire() as conn:
        for t in ("CITES", "PART_OF", "Document", "Collection"):
            await conn.execute(f'DELETE FROM "{TENANT}"."{t}";')
        await conn.execute("DELETE FROM graphos_meta.changes WHERE tenant = $1;", TENANT)
    await writes.upsert_nodes(pool, tenant=TENANT, label="Document", rows=[
        {"id": i, "props": {}} for i in ("a", "b", "c", "shared", QUOTED)
    ])
    await writes.upsert_nodes(pool, tenant=TENANT, label="Collection", rows=[
        {"id": "shared", "props": {}}, {"id": "box", "props": {}},
    ])
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Document", dst_label="Document", edge_type="CITES",
        rows=[{"src_id": "a", "dst_id": "b", "props": {}},
              {"src_id": "b", "dst_id": "c", "props": {}},
              {"src_id": "c", "dst_id": QUOTED, "props": {}}],
    )
    await writes.upsert_edges(
        pool, tenant=TENANT, src_label="Document", dst_label="Collection", edge_type="PART_OF",
        rows=[{"src_id": "a", "dst_id": "box", "props": {}},
              {"src_id": "shared", "dst_id": "shared", "props": {}}],
    )


async def _nodes(pool: asyncpg.Pool) -> set[tuple[str, str]]:
    rows = await pool.fetch(
        f"SELECT c.relname AS label, v.properties::text::jsonb ->> 'id' AS id "
        f'FROM "{TENANT}"."_ag_label_vertex" v JOIN pg_class c ON c.oid = v.tableoid;'
    )
    return {(r["label"], r["id"]) for r in rows}


async def _edge_count(pool: asyncpg.Pool) -> int:
    return int(await pool.fetchval(f'SELECT count(*) FROM "{TENANT}"."_ag_label_edge";'))


async def _deletes(pool: asyncpg.Pool) -> list[tuple[Any, ...]]:
    async with pool.acquire() as conn:
        page = await changes.read(conn, TENANT, None, 1000)
    return [(c.label, c.ids, c.count, c.detail) for c in page.changes if c.op == "delete"]


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _seed(db_pool)
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://test/v1/{TENANT}/graph",
    ) as client:
        yield client


# ── writes ─────────────────────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_deletes_the_named_nodes_and_their_edges_only(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    before = await _nodes(db_pool)
    refs = [("Document", "a"), ("Document", "b"), ("Collection", "shared"),
            ("Document", "a"), ("Document", "nope"), ("Collection", "a")]

    assert await writes.delete_nodes_by_ref(db_pool, tenant=TENANT, refs=refs) == (3, 4)
    # a->b, b->c, a->box and shared->shared go; c->QUOTED stays.
    assert await _nodes(db_pool) == before - {
        ("Document", "a"), ("Document", "b"), ("Collection", "shared"),
    }
    assert ("Document", "shared") in await _nodes(db_pool)
    assert await _edge_count(db_pool) == 1
    # Idempotent.
    assert await writes.delete_nodes_by_ref(db_pool, tenant=TENANT, refs=refs) == (0, 0)


@requires_db
@pytest.mark.asyncio
async def test_records_one_change_per_label(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    await writes.delete_nodes_by_ref(db_pool, tenant=TENANT, refs=[
        ("Document", QUOTED), ("Document", "c"), ("Collection", "box"), ("Collection", "nope"),
    ])
    # Labels in sorted order; edges_removed is each label's statement (a->box,
    # then b->c and c->QUOTED).
    assert await _deletes(db_pool) == [
        ("Collection", ["box"], 1, {"edges_removed": 1}),
        ("Document", sorted(["c", QUOTED]), 2, {"edges_removed": 2}),
    ]
    await writes.delete_nodes_by_ref(db_pool, tenant=TENANT, refs=[("Document", "nope")])
    assert len(await _deletes(db_pool)) == 2


@requires_db
@pytest.mark.asyncio
async def test_a_failure_on_one_label_deletes_nothing(
    db_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(db_pool)
    before, edges = await _nodes(db_pool), await _edge_count(db_pool)
    record = changes.record
    calls: list[str] = []

    async def fail_second(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs["label"])
        if len(calls) > 1:
            raise RuntimeError("second label fails")
        await record(*args, **kwargs)

    monkeypatch.setattr(writes.changes, "record", fail_second)
    with pytest.raises(RuntimeError):
        await writes.delete_nodes_by_ref(db_pool, tenant=TENANT, refs=[
            ("Collection", "box"), ("Document", "a"),
        ])
    assert calls == ["Collection", "Document"]
    assert (await _nodes(db_pool), await _edge_count(db_pool)) == (before, edges)
    assert await _deletes(db_pool) == []


@requires_db
@pytest.mark.asyncio
async def test_the_id_match_is_served_by_the_unique_id_index(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    async with db_pool.acquire() as conn, conn.transaction():
        # On a table this small the planner would rightly pick a seq scan;
        # this checks the predicate can use the index at all.
        await conn.execute("SET LOCAL enable_seqscan = off;")
        plan = await conn.fetchval(                 # JSON, as text
            f'EXPLAIN (FORMAT JSON) SELECT id FROM ONLY "{TENANT}"."Document" '
            f"WHERE {lookup.IDS_MATCH};",
            [json.dumps("a"), json.dumps("b")],
        )
    assert '"Index Name": "Document_id_unique"' in plan
    assert "Seq Scan" not in plan


# ── HTTP ───────────────────────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_post_nodes_delete(api: httpx.AsyncClient, db_pool: asyncpg.Pool) -> None:
    body = {"nodes": [{"label": "Document", "id": "b"}, {"label": "Document", "id": QUOTED},
                      {"label": "Document", "id": "b"}, {"label": "Collection", "id": "nope"}]}
    resp = await api.post("/nodes:delete", json=body)
    assert resp.status_code == HTTPStatus.OK, resp.text
    got = resp.json()
    assert (got["tenant"], got["nodes_requested"], got["nodes_deleted"], got["edges_deleted"]) == (
        TENANT, 4, 2, 3,
    )
    assert isinstance(got["elapsed_ms"], int)
    assert ("Document", "b") not in await _nodes(db_pool)

    again = (await api.post("/nodes:delete", json=body)).json()
    assert (again["nodes_deleted"], again["edges_deleted"]) == (0, 0)


@requires_db
@pytest.mark.asyncio
async def test_post_nodes_delete_refuses_an_unregistered_label(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    before = await _nodes(db_pool)
    resp = await api.post("/nodes:delete", json={"nodes": [
        {"label": "Document", "id": "a"}, {"label": "Person", "id": "a"},
    ]})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "'Person' is not registered" in resp.json()["detail"]
    assert await _nodes(db_pool) == before
    resp = await api.post("/nodes:delete", json={"nodes": []})
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@requires_db
@pytest.mark.asyncio
async def test_a_parent_label_takes_in_its_child_labels_nodes(db_pool: asyncpg.Pool) -> None:
    tenant = f"{TENANT}_tree"
    await register_tenant(db_pool, TenantSpec(
        name=tenant, vertex_labels=("Asset", "Server"), edge_labels=(),
        label_hierarchy=(("Server", "Asset"),),
    ))
    async with db_pool.acquire() as conn:
        for t in ("Server", "Asset"):
            await conn.execute(f'DELETE FROM "{tenant}"."{t}";')
    for label, node_id in (("Asset", "a1"), ("Server", "s1")):
        await writes.upsert_nodes(
            db_pool, tenant=tenant, label=label, rows=[{"id": node_id, "props": {}}],
        )

    refs = [("Asset", "a1"), ("Asset", "s1")]
    assert await writes.delete_nodes_by_ref(db_pool, tenant=tenant, refs=refs) == (2, 0)
    assert await db_pool.fetchval(f'SELECT count(*) FROM "{tenant}"."_ag_label_vertex";') == 0

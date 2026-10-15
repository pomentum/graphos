"""Replacing a run leaves no derived edge whose reason is gone.

DB-gated. Documents carry a run id and tags; a resolve rule links each
document to the topics its tags name. The run is loaded again with tags
removed and added:

* upserting the new rows and resolving again only adds, so the edges of the
  removed tags stay;
* deleting the run's nodes, loading the run and resolving again gives
  exactly the graph a fresh load gives, nodes and edges alike.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos.app import create_app
from graphos.bootstrap import forget
from graphos.config import Settings

from .conftest import requires_db

RELOADED = "graphos_test_run_replace"
FRESH = "graphos_test_run_replace_fresh"
RUN = "run-2"

TOPICS = [{"id": t, "label": "Topic", "properties": {"name": t, "run_id": "ref"}}
          for t in ("a", "b", "c")]
BEFORE = {"d1": ["a", "b"], "d2": ["b"]}
AFTER = {"d1": ["b", "c"], "d2": []}


def _docs(tags: dict[str, list[str]]) -> list[dict[str, Any]]:
    return [{"id": d, "label": "Doc", "properties": {"name": d, "run_id": RUN, "tags": t}}
            for d, t in tags.items()]


LINK = {"src_id": "d1", "src_label": "Doc", "dst_id": "d2", "dst_label": "Doc",
        "type": "LINKS", "properties": {"run_id": RUN}}


async def _drop(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for tenant in (RELOADED, FRESH):
            if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", tenant):
                await conn.execute(f"SELECT drop_graph('{tenant}', true);")
            for table in ("tenants", "unregistered_tenants"):
                await conn.execute(f"DELETE FROM graphos_meta.{table} WHERE name = $1;", tenant)
            forget(tenant)


async def _ok(resp: httpx.Response) -> dict[str, Any]:
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()


async def _load(api: httpx.AsyncClient, tenant: str, docs: list[dict[str, Any]]) -> None:
    g = f"/v1/{tenant}/graph"
    await _ok(await api.post(f"{g}/nodes", json={"nodes": docs}))
    await _ok(await api.post(f"{g}/edges", json={"edges": [LINK]}))
    await _ok(await api.post(f"{g}/edges:resolve", json={}))


async def _content(pool: asyncpg.Pool, tenant: str) -> tuple[list[str], list[str]]:
    """Every node and edge of ``tenant`` as text, sorted: what a checksum covers."""
    async with pool.acquire() as conn:
        nodes = await conn.fetch(
            f"SELECT c.relname || '|' || v.properties::text AS line "
            f'FROM "{tenant}"._ag_label_vertex v JOIN pg_class c ON c.oid = v.tableoid;',
        )
        edges = await conn.fetch(
            f"SELECT c.relname || '|' || (s.properties -> '\"id\"'::agtype)::text || '|' "
            f"|| (d.properties -> '\"id\"'::agtype)::text || '|' || e.properties::text AS line "
            f'FROM "{tenant}"._ag_label_edge e JOIN pg_class c ON c.oid = e.tableoid '
            f'JOIN "{tenant}"._ag_label_vertex s ON s.id = e.start_id '
            f'JOIN "{tenant}"._ag_label_vertex d ON d.id = e.end_id;',
        )
    return sorted(r["line"] for r in nodes), sorted(r["line"] for r in edges)


def _tagged(edges: list[str]) -> list[str]:
    return [e for e in edges if e.startswith("TAGGED|")]


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _drop(db_pool)
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        for tenant in (RELOADED, FRESH):
            body = {
                "name": tenant,
                "vertex_labels": ["Doc", "Topic"],
                "edge_labels": [
                    "LINKS",
                    {"name": "TAGGED",
                     "resolve_rule": {"src_has_array": "tags", "dst_value_from": "name"}},
                ],
            }
            await _ok(await client.post("/v1/tenants", json=body))
            await _ok(await client.post(f"/v1/{tenant}/graph/nodes", json={"nodes": TOPICS}))
        await _load(client, RELOADED, _docs(BEFORE))
        await _load(client, FRESH, _docs(AFTER))
        yield client
    await _drop(db_pool)


@requires_db
@pytest.mark.asyncio
async def test_an_upsert_keeps_the_derived_edges_of_removed_tags(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    await _load(api, RELOADED, _docs(AFTER))
    _, edges = await _content(db_pool, RELOADED)
    _, fresh = await _content(db_pool, FRESH)
    stale = sorted(set(_tagged(edges)) - set(_tagged(fresh)))
    assert [s.split("|")[1:3] for s in stale] == [["d1", "a"], ["d2", "b"]]


@requires_db
@pytest.mark.asyncio
async def test_delete_load_resolve_gives_what_a_fresh_load_gives(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    head = (await _ok(await api.get(f"/v1/{RELOADED}/changes/head")))["cursor"]
    deleted = await _ok(await api.delete(
        f"/v1/{RELOADED}/graph/nodes", params={"property_key": "run_id", "property_value": RUN},
    ))
    assert (deleted["nodes_deleted"], deleted["edges_deleted"]) == (2, 4)
    await _load(api, RELOADED, _docs(AFTER))

    assert await _content(db_pool, RELOADED) == await _content(db_pool, FRESH)

    feed = (await _ok(await api.get(f"/v1/{RELOADED}/changes", params={"after": head})))
    delete = next(c for c in feed["changes"] if c["op"] == "delete")
    assert (delete["count"], delete["detail"]["edges_removed"]) == (2, 4)
    assert delete["detail"]["where"] == {"run_id": RUN}
    assert [c["op"] for c in feed["changes"]].count("resolve") == 1

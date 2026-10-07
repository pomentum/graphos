"""DB tests: the resolver's property catalogue is current after writes and cheap to read.

DB-gated.  The catalogue reads ``graphos_meta.node_properties``, which every
node write adds to in its own transaction (``graphos.node_properties``).
Covered: each write path records its names, inheritance pruning, the
one-time backfill of a graph that predates the record, the rebuild that
drops names no node carries, the endpoint, and an ``edges:resolve`` over
data written after registration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import node_properties, writes
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, forget, register_tenant
from graphos.config import Settings
from graphos.resolver.catalogue import catalogue_for_properties, refresh_catalogue

from .conftest import requires_db

TENANT = "graphos_test_catalogue"
SPEC = TenantSpec(
    name=TENANT,
    vertex_labels=("Person", "Employee", "Doc", "Tag"),
    edge_labels=("TAGGED",),
    label_hierarchy=(("Employee", "Person"),),
    resolve_rules=(("TAGGED", {"src_has_array": "tag_keys", "dst_value_from": "key"}),),
)


async def _drop(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", TENANT):
            await conn.execute(f"SELECT drop_graph('{TENANT}', true);")
        await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", TENANT)
    forget(TENANT)


@pytest_asyncio.fixture
async def tenant(db_pool: asyncpg.Pool) -> AsyncIterator[str]:
    await _drop(db_pool)
    await register_tenant(db_pool, SPEC)
    yield TENANT
    forget(TENANT)


async def _seed(pool: asyncpg.Pool) -> None:
    await writes.upsert_nodes(pool, tenant=TENANT, label="Person", rows=[
        {"id": "p1", "props": {"name": "Ada", "email": "ada@example.org"}},
    ])
    await writes.upsert_nodes(pool, tenant=TENANT, label="Employee", rows=[
        {"id": "e1", "props": {"name": "Grace", "badge": 7, "tag_keys": ["db", "ui"]}},
    ])
    await writes.upsert_nodes(pool, tenant=TENANT, label="Doc", rows=[
        {"id": "d1", "props": {"title": "Notes", "tag_keys": ["db"]}},
    ])
    await writes.upsert_nodes(pool, tenant=TENANT, label="Tag", rows=[
        {"id": "t1", "props": {"key": "db"}}, {"id": "t2", "props": {"key": "ui"}},
    ])


async def _recorded(pool: asyncpg.Pool) -> dict[str, set[str]]:
    async with pool.acquire() as conn:
        return await node_properties.labels_by_property(conn, TENANT)


@requires_db
@pytest.mark.asyncio
async def test_a_new_graph_starts_complete_and_empty(db_pool: asyncpg.Pool, tenant: str) -> None:
    async with db_pool.acquire() as conn:
        assert await node_properties.is_complete(conn, tenant)
    assert await _recorded(db_pool) == {}
    assert (await catalogue_for_properties(db_pool, tenant, ["name"])).by_property == {}


@requires_db
@pytest.mark.asyncio
async def test_every_node_write_records_its_names(db_pool: asyncpg.Pool, tenant: str) -> None:
    await _seed(db_pool)
    recorded = await _recorded(db_pool)
    assert recorded["name"] == {"Person", "Employee"}
    assert recorded["tag_keys"] == {"Employee", "Doc"}
    assert recorded["id"] == {"Person", "Employee", "Doc", "Tag"}
    await writes.merge_node_properties(
        db_pool, tenant=tenant, external_id="t1", properties={"color": "red"},
    )
    await writes.patch_node(
        db_pool, tenant=tenant, external_id="d1", patch={"owner": "ada", "title": None},
    )
    async with db_pool.acquire() as conn:
        gid = await conn.fetchval(f'SELECT id::text::bigint FROM "{tenant}"."Doc" LIMIT 1;')
    await writes.set_node_communities(db_pool, tenant=tenant, assignments=[(gid, 3)])
    recorded = await _recorded(db_pool)
    assert recorded["color"] == {"Tag"}
    assert recorded["owner"] == {"Doc"}
    assert recorded["community_id"] == {"Doc"}


@requires_db
@pytest.mark.asyncio
async def test_lookup_is_current_and_prunes_inheritance_parents(
    db_pool: asyncpg.Pool, tenant: str,
) -> None:
    await _seed(db_pool)
    wanted = ["name", "tag_keys", "key", "missing", "bad-name"]
    cat = await catalogue_for_properties(db_pool, tenant, wanted)
    # Employee inherits from Person: Person is pruned where Employee carries the property.
    assert cat.labels_for("name") == {"Employee"}
    assert cat.labels_for("tag_keys") == {"Employee", "Doc"}
    assert cat.labels_for("key") == {"Tag"}
    assert set(cat.by_property) == {"name", "tag_keys", "key"}
    await writes.upsert_nodes(
        db_pool, tenant=tenant, label="Doc", rows=[{"id": "d2", "props": {"key": "x"}}],
    )
    now = await catalogue_for_properties(db_pool, tenant, ["key"])
    assert now.labels_for("key") == {"Doc", "Tag"}
    # A recorded label whose table is gone (dropped by hand) is left out.
    async with db_pool.acquire() as conn:
        await node_properties.note(conn, tenant, "Gone", ["key"])
    again = await catalogue_for_properties(db_pool, tenant, ["key"])
    assert again.labels_for("key") == {"Doc", "Tag"}


@requires_db
@pytest.mark.asyncio
async def test_an_older_graph_is_backfilled_once(db_pool: asyncpg.Pool, tenant: str) -> None:
    await _seed(db_pool)
    expected = await _recorded(db_pool)
    # As if the graph predated the record: nothing recorded, not complete.
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM graphos_meta.node_properties WHERE tenant = $1;", tenant)
        await conn.execute(
            "DELETE FROM graphos_meta.node_properties_complete WHERE tenant = $1;", tenant,
        )
    cat = await catalogue_for_properties(db_pool, tenant, ["tag_keys"])
    assert cat.labels_for("tag_keys") == {"Employee", "Doc"}
    assert await _recorded(db_pool) == expected
    async with db_pool.acquire() as conn:
        assert await node_properties.is_complete(conn, tenant)


@requires_db
@pytest.mark.asyncio
async def test_a_rebuild_drops_names_no_node_carries(db_pool: asyncpg.Pool, tenant: str) -> None:
    await _seed(db_pool)
    await writes.patch_node(db_pool, tenant=tenant, external_id="p1", patch={"email": None})
    assert "Person" in (await _recorded(db_pool))["email"]    # the record only grows
    full = await refresh_catalogue(db_pool, tenant, rebuild=True)
    assert "email" not in full.by_property
    assert full.labels_for("badge") == {"Employee"}


@requires_db
@pytest.mark.asyncio
async def test_endpoint_and_resolve_see_data_written_after_registration(
    db_pool: asyncpg.Pool, tenant: str,
) -> None:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        await _seed(db_pool)
        params = [("property", "tag_keys"), ("property", "key")]
        body = (await api.get(f"/v1/{tenant}/graph/catalogue", params=params)).json()
        assert body["by_property"] == {"key": ["Tag"], "tag_keys": ["Doc", "Employee"]}
        full = (await api.get(f"/v1/{tenant}/graph/catalogue")).json()["by_property"]
        assert full["badge"] == ["Employee"]
        resolved = (await api.post(f"/v1/{tenant}/graph/edges:resolve", json={})).json()
        assert resolved["total_edges_written"] == 3   # e1→db, e1→ui, d1→db

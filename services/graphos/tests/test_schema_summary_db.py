"""DB tests: ``GET /v1/{tenant}/schema/summary`` (ADR-0002 §1).

DB-gated.  The API is driven in-process through httpx's ASGI transport with
the test pool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import schema_rename, writes
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, forget, register_tenant, registered_tenants
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_summary"


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", TENANT):
            await conn.execute(f"SELECT drop_graph('{TENANT}', true);")
        for table, column in (
            ("tenants", "name"), ("edge_endpoints", "tenant"), ("changes", "tenant"),
            ("change_horizon", "tenant"), ("node_properties", "tenant"),
        ):
            await conn.execute(f"DELETE FROM graphos_meta.{table} WHERE {column} = $1;", TENANT)
    forget(TENANT)


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _fresh(db_pool)
    await register_tenant(db_pool, TenantSpec(
        name=TENANT, vertex_labels=("Person", "Doc"), edge_labels=("AUTHORED",),
        property_schema={"mode": "warn", "vertex": {"Person": {"properties": {
            "name": {"type": "string", "required": True}}}}},
    ))
    await writes.upsert_nodes(db_pool, tenant=TENANT, label="Person", rows=[
        {"id": f"p{i}", "props": {"name": f"person {i}", "status": "ACT" if i % 3 else "OLD"}}
        for i in range(30)
    ])
    await writes.upsert_nodes(db_pool, tenant=TENANT, label="Doc", rows=[
        {"id": "d1", "props": {"title": "one"}},
    ])
    await writes.upsert_edges(
        db_pool, tenant=TENANT, src_label="Person", dst_label="Doc", edge_type="AUTHORED",
        rows=[{"src_id": "p1", "dst_id": "d1", "props": {}}],
    )
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://test/v1/{TENANT}",
    ) as client:
        yield client
    await _fresh(db_pool)


@requires_db
@pytest.mark.asyncio
async def test_the_summary_lists_what_the_data_holds_and_is_cached(
    api: httpx.AsyncClient,
) -> None:
    r = await api.get("/schema/summary")
    assert r.status_code == HTTPStatus.OK, r.text
    body = r.json()
    assert [(x["label"], x["count"]) for x in body["labels"]] == [("Person", 30), ("Doc", 1)]
    person = {p["name"]: p for p in body["labels"][0]["properties"]}
    assert (person["name"]["type"], person["name"]["required"]) == ("string", True)
    assert person["name"]["values"] is None and person["name"]["distinct"] == 30  # noqa: PLR2004
    assert {v["value"]: v["count"] for v in person["status"]["values"]} == {"ACT": 20, "OLD": 10}
    assert body["links"] == [
        {"src_label": "Person", "type": "AUTHORED", "dst_label": "Doc", "count": 1},
    ]
    assert body["cached"] is False and '"ACT" 20' in body["text"]   # strings quoted as JSON
    again = (await api.get("/schema/summary")).json()
    assert again["cached"] is True and again["labels"] == body["labels"]
    text = await api.get("/schema/summary", params={"format": "text"})
    assert text.headers["content-type"].startswith("text/plain")
    assert text.text == body["text"]


@requires_db
@pytest.mark.asyncio
async def test_the_summary_names_what_is_deprecated_and_what_was_renamed(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    await register_tenant(db_pool, TenantSpec(
        name=TENANT, vertex_labels=("Person", "Doc"), edge_labels=("AUTHORED",),
        deprecated={"vertex": {
            "Doc": {"deprecated": True, "replaced_by": "Person", "note": "until 2027"},
            "Person": {"properties": {"status": {"replaced_by": "state"}}},
        }},
    ))
    body = (await api.get("/schema/summary")).json()
    assert body["deprecated"] == [
        {"kind": "label", "name": "Doc", "on": None, "replaced_by": "Person",
         "note": "until 2027"},
        {"kind": "property", "name": "status", "on": "Person", "replaced_by": "state",
         "note": None},
    ]
    assert body["renames"] == []
    assert ("Deprecated (still readable; do not use in new writes): label Doc, use Person "
            "(until 2027); property Person.status, use state") in body["text"]
    await schema_rename.rename(
        db_pool, registered_tenants()[TENANT], "edge_type", "AUTHORED", "WROTE", by="olive",
    )
    after = (await api.get("/schema/summary")).json()
    assert after["cached"] is False
    assert [(r["kind"], r["from"], r["to"], r["status"]) for r in after["renames"]] == [
        ("edge_type", "AUTHORED", "WROTE", "done"),
    ]
    assert "Renamed (the old names are gone): edge type AUTHORED -> WROTE (" in after["text"]
    assert [x["type"] for x in after["links"]] == ["WROTE"]

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

from graphos import writes
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, forget, register_tenant
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_summary"


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", TENANT):
            await conn.execute(f"SELECT drop_graph('{TENANT}', true);")
        await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", TENANT)
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
    assert body["cached"] is False and "'ACT' 20" in body["text"]
    again = (await api.get("/schema/summary")).json()
    assert again["cached"] is True and again["labels"] == body["labels"]
    text = await api.get("/schema/summary", params={"format": "text"})
    assert text.headers["content-type"].startswith("text/plain")
    assert text.text == body["text"]

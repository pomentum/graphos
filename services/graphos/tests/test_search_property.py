"""DB tests: unindexed ``GET /search`` over a chosen property.

Without a search index, search is a substring match. It was always on
``name``; ``property`` picks another one, for tenants whose nodes are
known by a path or a qualified name rather than a name.
"""

from __future__ import annotations

from http import HTTPStatus

import asyncpg
import httpx
import pytest

from graphos import writes
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_search_property"


async def _seed(pool: asyncpg.Pool) -> None:
    await register_tenant(
        pool, TenantSpec(name=TENANT, vertex_labels=("File", "Class"), edge_labels=()),
    )
    async with pool.acquire() as conn:
        for label in ("File", "Class"):
            await conn.execute(f'DELETE FROM "{TENANT}"."{label}";')
    await writes.upsert_nodes(pool, tenant=TENANT, label="File", rows=[
        {"id": "f1", "props": {"name": "Invoice.java", "path": "src/billing/Invoice.java"}},
        {"id": "f2", "props": {"name": "Cart.java", "path": "src/cart/Cart.java"}},
    ])
    await writes.upsert_nodes(pool, tenant=TENANT, label="Class", rows=[
        {"id": "c1", "props": {"name": "Invoice", "qualified_name": "com.acme.billing.Invoice"}},
    ])


def _client(pool: asyncpg.Pool) -> httpx.AsyncClient:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = pool
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://test/v1/{TENANT}/graph",
    )


async def _ids(api: httpx.AsyncClient, **params: str) -> list[str]:
    resp = await api.get("/search", params=params)
    assert resp.status_code == HTTPStatus.OK, resp.text
    return sorted(hit["properties"]["id"] for hit in resp.json()["nodes"])


@requires_db
@pytest.mark.asyncio
async def test_search_matches_the_chosen_property(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    async with _client(db_pool) as api:
        assert await _ids(api, q="invoice") == ["c1", "f1"]                 # name, by default
        assert await _ids(api, q="billing") == []
        assert await _ids(api, q="BILLING", property="path") == ["f1"]       # case-insensitive
        assert await _ids(api, q="billing", property="qualified_name") == ["c1"]
        assert await _ids(api, q="src/", property="path", label="File") == ["f1", "f2"]
        assert await _ids(api, q="x", property="absent") == []


@requires_db
@pytest.mark.asyncio
async def test_search_rejects_an_unsafe_property_name(db_pool: asyncpg.Pool) -> None:
    await _seed(db_pool)
    async with _client(db_pool) as api:
        resp = await api.get("/search", params={"q": "x", "property": "name' OR 1=1 --"})
        assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

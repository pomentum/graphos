"""DB tests: node ids that contain ``/`` are addressable.

Ids are the consumer's own keys, and file paths (``src/a/B.java``) are a
common one. A client encodes ``/`` as ``%2F`` in the URL, but the server
decodes the path before routing, so ``/nodes/{id}`` must accept slashes.
"""

from __future__ import annotations

from http import HTTPStatus
from urllib.parse import quote

import asyncpg
import httpx
import pytest

from graphos import writes
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_node_id_paths"
FILE_ID = "repo/src/main/java/com/example/Invoice.java::__file__"


async def _seed(pool: asyncpg.Pool) -> None:
    await register_tenant(pool, TenantSpec(name=TENANT, vertex_labels=("File",), edge_labels=()))
    async with pool.acquire() as conn:
        await conn.execute(f'DELETE FROM "{TENANT}"."File";')
    await writes.upsert_nodes(
        pool, tenant=TENANT, label="File",
        rows=[{"id": FILE_ID, "props": {"name": "Invoice.java"}}],
    )


def _client(pool: asyncpg.Pool) -> httpx.AsyncClient:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = pool
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://test/v1/{TENANT}/graph",
    )


@requires_db
@pytest.mark.asyncio
async def test_node_with_slashes_in_its_id_can_be_read_patched_and_deleted(
    db_pool: asyncpg.Pool,
) -> None:
    await _seed(db_pool)
    path = f"/nodes/{quote(FILE_ID, safe='')}"
    async with _client(db_pool) as api:
        got = await api.get(path, params={"label": "File"})
        assert got.status_code == HTTPStatus.OK, got.text
        assert got.json()["properties"]["name"] == "Invoice.java"

        patched = await api.patch(
            path, params={"label": "File"}, json={"lines": 10},
            headers={"If-Match": got.headers["ETag"]},
        )
        assert patched.status_code == HTTPStatus.OK, patched.text

        merged = await api.post(f"{path}/properties", json={"properties": {"owner": "x"}})
        assert merged.status_code == HTTPStatus.OK, merged.text

        again = await api.get(path, params={"label": "File"})
        props = again.json()["properties"]
        assert (props["lines"], props["owner"]) == (10, "x")

        deleted = await api.delete(path)
        assert deleted.status_code == HTTPStatus.OK, deleted.text
        assert (await api.get(path, params={"label": "File"})).status_code == HTTPStatus.NOT_FOUND

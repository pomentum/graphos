"""DB tests: graph stats count every node once, label hierarchies included.

A child label's rows also belong to its parent label's table (Postgres
inheritance), so per-label counts overlap: the parent's count includes its
children. The totals must not add them up.
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

TENANT = "graphos_test_stats_totals"


@requires_db
@pytest.mark.asyncio
async def test_totals_count_each_node_once(db_pool: asyncpg.Pool) -> None:
    await register_tenant(db_pool, TenantSpec(
        name=TENANT, vertex_labels=("Person", "Employee"), edge_labels=("KNOWS",),
        label_hierarchy=(("Employee", "Person"),),
    ))
    async with db_pool.acquire() as conn:
        for t in ("KNOWS", "Employee", "Person"):
            await conn.execute(f'DELETE FROM "{TENANT}"."{t}";')
    await writes.upsert_nodes(db_pool, tenant=TENANT, label="Person", rows=[
        {"id": "p1", "props": {}},
    ])
    await writes.upsert_nodes(db_pool, tenant=TENANT, label="Employee", rows=[
        {"id": "e1", "props": {}}, {"id": "e2", "props": {}},
    ])
    await writes.upsert_edges(
        db_pool, tenant=TENANT, src_label="Employee", dst_label="Person", edge_type="KNOWS",
        rows=[{"src_id": "e1", "dst_id": "p1", "props": {}}],
    )

    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        resp = await api.get(f"/v1/{TENANT}/graph/stats")
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    counts = {r["label"]: r["count"] for r in body["labels"]}
    assert counts["Person"] == 3          # its own node and its children's, as before
    assert counts["Employee"] == 2
    assert (body["n_vertices"], body["n_edges"]) == (3, 1)

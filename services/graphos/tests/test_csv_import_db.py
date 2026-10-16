"""DB tests for the CSV import: ``POST /v1/{tenant}/graph/nodes:csv`` and
``…/edges:csv`` writing for real, driven in-process through httpx's ASGI
transport with the test pool.  DB-gated.
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

from graphos import changes
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_csv_import"
CSV = {"Content-Type": "text/csv"}


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await register_tenant(db_pool, TenantSpec(
        name=TENANT, vertex_labels=("Person", "Team"), edge_labels=("MEMBER_OF",),
        property_schema={"vertex": {"Person": {
            "mode": "strict",
            "properties": {
                "name": {"type": "string", "required": True}, "age": {"type": "integer"},
            },
        }}},
    ))
    async with db_pool.acquire() as conn:
        for t in ("MEMBER_OF", "Person", "Team"):
            await conn.execute(f'DELETE FROM "{TENANT}"."{t}";')
        await conn.execute("DELETE FROM graphos_meta.changes WHERE tenant = $1;", TENANT)
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://test/v1/{TENANT}/graph",
    ) as client:
        yield client


async def _people(pool: asyncpg.Pool) -> dict[str, dict[str, Any]]:
    rows = await pool.fetch(f'SELECT properties::text::jsonb AS p FROM "{TENANT}"."Person";')
    out = {}
    for r in rows:
        props = json.loads(r["p"]) if isinstance(r["p"], str) else dict(r["p"])
        out[props.pop("id")] = props
    return out


@requires_db
@pytest.mark.asyncio
async def test_imports_nodes_merged_the_record_checked_not_the_row(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    # p1's second row lacks the required name: merged, the record has it.
    body = "id,name,age\np1,Ann,30\np1,,31\np2,,40\np3,Cy,x\n"
    resp = await api.post("/nodes:csv", params={"label": "Person"}, content=body, headers=CSV)
    assert resp.status_code == HTTPStatus.OK, resp.text
    answer = resp.json()
    assert (answer["records"], answer["written"], answer["rows_invalid"]) == (3, 2, 1)
    assert answer["errors"] == [
        "row 3: property_schema: Person.name: required",
    ]
    assert await _people(db_pool) == {"p1": {"name": "Ann", "age": 31}, "p3": {"name": "Cy"}}
    async with db_pool.acquire() as conn:
        page = await changes.read(conn, TENANT, None, 100)
    assert [(c.op, c.kind, c.label, c.count) for c in page.changes] == [
        ("upsert", "node", "Person", 2),
    ]


@requires_db
@pytest.mark.asyncio
async def test_a_dry_run_answers_as_the_import_would_and_writes_nothing(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    await api.post("/nodes:csv", params={"label": "Team"}, content="id\nt1\n", headers=CSV)
    await api.post(
        "/nodes:csv", params={"label": "Person"}, content="id,name\np1,Ann\n", headers=CSV,
    )
    body = "src;dst;role\np1;t1;lead\np1;t1;owner\np1;t9;x\n"
    params = {"type": "MEMBER_OF", "src_label": "Person", "dst_label": "Team"}
    dry = await api.post(
        "/edges:csv", params={**params, "dry_run": "true"}, content=body, headers=CSV,
    )
    assert dry.status_code == HTTPStatus.OK, dry.text
    assert (dry.json()["written"], dry.json()["edges_skipped"]) == (1, 1)
    count = f'SELECT count(*) FROM "{TENANT}"."MEMBER_OF";'
    assert await db_pool.fetchval(count) == 0

    real = await api.post("/edges:csv", params=params, content=body, headers=CSV)
    assert (real.json()["written"], real.json()["edges_skipped"]) == (1, 1)
    props = await db_pool.fetchval(f'SELECT properties::text FROM "{TENANT}"."MEMBER_OF";')
    assert '"role": "owner"' in props
    # Sent again: the same graph (upserts).
    again = await api.post("/edges:csv", params=params, content=body, headers=CSV)
    assert again.json()["written"] == 1
    assert await db_pool.fetchval(count) == 1

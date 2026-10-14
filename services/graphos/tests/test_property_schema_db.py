"""DB tests: writes held to a tenant's property schema, on every write path.

Strict refuses (a batch with 422 and nothing written; a bulk or snapshot line
counted invalid); warn writes and counts; a patch is checked as it leaves
the row; a snapshot import can report instead of refusing.
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

from graphos.app import create_app
from graphos.bootstrap import forget
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_props"
G = f"/v1/{TENANT}/graph"
SCHEMA: dict[str, Any] = {
    "mode": "strict",
    "vertex": {
        "*": {"properties": {"source": {"type": "string", "required": True}}},
        "Person": {"properties": {
            "name": {"type": "string", "required": True},
            "email": {"type": "string", "pattern": "[^@]+@[^@]+", "recommended": True},
        }},
    },
    "edge": {"WROTE": {"properties": {"role": {"type": "string", "enum": ["lead", "co"]}}}},
}
ADA = {"id": "p1", "label": "Person", "properties": {"source": "crm", "name": "Ada"}}
DOC = {"id": "d1", "label": "Doc", "properties": {"source": "crm"}}


async def _drop(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", TENANT):
            await conn.execute(f"SELECT drop_graph('{TENANT}', true);")
        for table in ("tenants", "unregistered_tenants"):
            await conn.execute(f"DELETE FROM graphos_meta.{table} WHERE name = $1;", TENANT)
    forget(TENANT)


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _drop(db_pool)
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        body = {"name": TENANT, "vertex_labels": ["Person", "Doc"], "edge_labels": ["WROTE"],
                "property_schema": SCHEMA}
        assert (await client.post("/v1/tenants", json=body)).status_code == HTTPStatus.OK
        yield client
    await _drop(db_pool)


async def _mode(api: httpx.AsyncClient, mode: str) -> None:
    body = {"name": TENANT, "property_schema": {"mode": mode}}
    assert (await api.post("/v1/tenants", json=body)).status_code == HTTPStatus.OK


async def _count(pool: asyncpg.Pool, label: str) -> int:
    return int(await pool.fetchval(f'SELECT count(*) FROM "{TENANT}"."{label}";'))


def _ndjson(*rows: dict[str, Any]) -> bytes:
    return "\n".join(json.dumps(r) for r in rows).encode()


@requires_db
@pytest.mark.asyncio
async def test_strict_refuses_a_batch_whole_and_warn_writes_and_counts(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    nameless = {"id": "p2", "label": "Person", "properties": {"source": "crm", "email": "x"}}
    resp = await api.post(f"{G}/nodes", json={"nodes": [ADA, nameless]})
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    body = resp.json()
    assert body["total"] == 2
    assert {(v["index"], v["property"], v["rule"]) for v in body["violations"]} == {
        (1, "name", "required"), (1, "email", "pattern"),
    }
    assert await _count(db_pool, "Person") == 0              # nothing written

    resp = await api.post(f"{G}/nodes", json={"nodes": [ADA, DOC]})
    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["property_warnings"] == {"Person.email:recommended": 1}

    await _mode(api, "warn")
    resp = await api.post(f"{G}/nodes", json={"nodes": [nameless]})
    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["property_warnings"] == {
        "Person.name:required": 1, "Person.email:pattern": 1,
    }
    assert await _count(db_pool, "Person") == 2

    await _mode(api, "strict")
    bad = {"src_id": "p1", "src_label": "Person", "dst_id": "d1", "dst_label": "Doc",
           "type": "WROTE", "properties": {"role": "editor"}}
    resp = await api.post(f"{G}/edges", json={"edges": [bad]})
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert resp.json()["violations"][0]["rule"] == "enum"


@requires_db
@pytest.mark.asyncio
async def test_bulk_and_snapshot_lines_are_counted_invalid_or_reported(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    nameless = {"id": "p2", "label": "Person", "properties": {"source": "crm"}}
    resp = await api.post(f"{G}/nodes:bulk", content=_ndjson(ADA, nameless, DOC),
                          headers={"Content-Type": "application/x-ndjson"})
    body = resp.json()
    assert (body["rows_invalid"], body["nodes_upserted"]) == (1, 2)
    assert body["property_violations"] == {"Person.name:required": 1}
    assert body["errors"] == ["line 2: property_schema: Person.name: required"]

    edge = {"src_id": "p1", "src_label": "Person", "dst_id": "d1", "dst_label": "Doc",
            "type": "WROTE", "properties": {"role": 3}}
    resp = await api.post(f"{G}/edges:bulk", content=_ndjson(edge),
                          headers={"Content-Type": "application/x-ndjson"})
    assert resp.json()["property_violations"] == {"WROTE.role:type": 1}

    snapshot = _ndjson({"_kind": "node", **nameless}, {"_kind": "edge", **edge})
    resp = await api.post(f"{G}/snapshot-import", content=snapshot,
                          headers={"Content-Type": "application/x-ndjson"})
    body = resp.json()
    assert (body["nodes_upserted"], body["edges_upserted"]) == (0, 0)
    assert body["property_violations"] == {"Person.name:required": 1, "WROTE.role:type": 1}
    resp = await api.post(f"{G}/snapshot-import?properties=report", content=snapshot,
                          headers={"Content-Type": "application/x-ndjson"})
    body = resp.json()
    assert (body["nodes_upserted"], body["edges_upserted"]) == (1, 1)
    assert body["property_warnings"] == {
        "Person.name:required": 1, "Person.email:recommended": 1, "WROTE.role:type": 1,
    }


@requires_db
@pytest.mark.asyncio
async def test_a_patch_is_checked_as_it_leaves_the_row(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    assert (await api.post(f"{G}/nodes", json={"nodes": [ADA, DOC]})).status_code == HTTPStatus.OK
    merge_patch = {"content-type": "application/merge-patch+json"}
    before = await api.get(f"{G}/nodes/p1")
    resp = await api.patch(f"{G}/nodes/p1", content=json.dumps({"name": None}), headers=merge_patch)
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert resp.json()["violations"] == [{
        "id": "p1", "label": "Person", "property": "name", "rule": "required",
        "message": "required", "source": "Person",
    }]
    after = await api.get(f"{G}/nodes/p1")
    assert after.headers["etag"] == before.headers["etag"]      # unchanged
    nickname = json.dumps({"nickname": "A"})
    ok = await api.patch(f"{G}/nodes/p1", content=nickname, headers=merge_patch)
    assert ok.status_code == HTTPStatus.OK
    # The properties merge is checked on the merged result too.
    merged = await api.post(f"{G}/nodes/p1/properties", json={"properties": {"email": "nobody"}})
    assert merged.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    edge = {"src_id": "p1", "src_label": "Person", "dst_id": "d1", "dst_label": "Doc",
            "type": "WROTE", "properties": {"role": "lead"}}
    assert (await api.post(f"{G}/edges", json={"edges": [edge]})).status_code == HTTPStatus.OK
    resp = await api.patch(f"{G}/edges/WROTE?src=p1&dst=d1", content=json.dumps({"role": "x"}),
                           headers=merge_patch)
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@requires_db
@pytest.mark.asyncio
async def test_off_checks_nothing(api: httpx.AsyncClient) -> None:
    await _mode(api, "off")
    junk = {"id": "p9", "label": "Person", "properties": {"name": 42}}
    resp = await api.post(f"{G}/nodes", json={"nodes": [junk]})
    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["property_warnings"] == {}


@requires_db
@pytest.mark.asyncio
async def test_the_stored_rows_are_reported_as_if_every_rule_applied(
    api: httpx.AsyncClient,
) -> None:
    await _mode(api, "off")                                     # loaded unchecked
    rows = [ADA, DOC, {"id": "p2", "label": "Person", "properties": {"nmae": "Bob"}}]
    assert (await api.post(f"{G}/nodes", json={"nodes": rows})).status_code == HTTPStatus.OK
    edge = {"src_id": "p1", "src_label": "Person", "dst_id": "d1", "dst_label": "Doc",
            "type": "WROTE", "properties": {"role": "editor", "at": 2026}}
    assert (await api.post(f"{G}/edges", json={"edges": [edge]})).status_code == HTTPStatus.OK

    report = (await api.post(f"{G}/properties:check", json={})).json()
    assert report["mode"] == "off"
    assert (report["rows"], report["rows_with_errors"]) == (4, 2)
    person = next(r for r in report["labels"] if r["name"] == "Person")
    assert person["errors"] == {"*.source:required": 1, "Person.name:required": 1}
    assert person["warnings"] == {"Person.email:recommended": 2}
    assert person["examples"]["Person.name:required"] == ["p2"]
    assert person["undeclared"] == {"nmae": 1}
    wrote = next(r for r in report["labels"] if r["name"] == "WROTE")
    assert wrote["errors"] == {"WROTE.role:enum": 1}
    assert wrote["examples"]["WROTE.role:enum"] == ["p1 -> d1"]
    assert wrote["undeclared"] == {"at": 1}

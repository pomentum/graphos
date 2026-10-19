"""DB tests: renaming a property by a background job (ADR-0009 §3).

The route queues the job and marks the rename running in one transaction;
these tests then run the job as the runner would.  Every row gets the new
name and a new revision; the definitions, indexes, styles and deprecations
follow; writes naming the old name warn while it runs and are refused after.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any
from uuid import UUID

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import jobs, schema_rename
from graphos.app import create_app
from graphos.auth import StaticAuthenticator
from graphos.bootstrap import forget, registered_tenants
from graphos.config import Settings
from graphos.identity import Principal

from .conftest import requires_db

NAME = "graphos_test_prop_rename"
SCHEMA: dict[str, Any] = {
    "name": NAME,
    "vertex_labels": ["Docu", "Report", "Tag"],
    "edge_labels": ["CITES", {"name": "TAGGED", "resolve_rule": {
        "src_has_array": "tags", "dst_value_from": "name",
    }}],
    "label_hierarchy": {"Report": "Docu"},
    "property_indexes": [{"label": "Docu", "properties": ["title"]}],
    "display": {"vertex": {"Docu": {"caption": "title"}}},
    "property_schema": {"vertex": {"Docu": {"properties": {"title": {"type": "string"}}}}},
    "deprecated": {"vertex": {"Docu": {"properties": {"title": {"replaced_by": "headline"}}}}},
}
PRINCIPALS = {
    "owner": Principal.static("olive", tenants={NAME: "owner"}),
    "maintainer": Principal.static("max", tenants={NAME: "maintain"}),
    "creator": Principal.static("svc", create_tenants=["graphos_test_*"]),
}


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", NAME):
            await conn.execute("SELECT drop_graph($1::name, true);", NAME)
        for table, column in (
            ("tenants", "name"), ("unregistered_tenants", "name"), ("edge_endpoints", "tenant"),
            ("tenant_settings", "tenant"), ("changes", "tenant"), ("change_horizon", "tenant"),
            ("jobs", "tenant"), ("saved_queries", "tenant"), ("node_properties", "tenant"),
        ):
            await conn.execute(f"DELETE FROM graphos_meta.{table} WHERE {column} = $1;", NAME)
    forget(NAME)


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _fresh(db_pool)
    app = create_app(Settings(db_password="placeholder"))
    app.state.pool = db_pool
    app.state.authenticator = StaticAuthenticator(PRINCIPALS)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        registered = await client.post("/v1/tenants", json=SCHEMA, headers=_as("creator"))
        assert registered.status_code == HTTPStatus.OK, registered.text
        wrote = await client.post(f"/v1/{NAME}/graph/nodes", headers=_as("owner"), json={
            "nodes": [
                {"id": "d1", "label": "Docu", "properties": {"title": "Notes", "kind": "memo"}},
                {"id": "d2", "label": "Docu", "properties": {"kind": "draft"}},
                {"id": "r1", "label": "Report", "properties": {"title": "Q3"}},
            ],
        })
        assert wrote.status_code == HTTPStatus.OK, wrote.text
        await client.post(f"/v1/{NAME}/graph/nodes", headers=_as("owner"), json={
            "nodes": [{"id": "t1", "label": "Tag", "properties": {"name": "x"}}],
        })
        edges = await client.post(f"/v1/{NAME}/graph/edges", headers=_as("owner"), json={
            "edges": [{"src_id": "r1", "src_label": "Report", "dst_id": "d1",
                       "dst_label": "Docu", "type": "CITES", "properties": {"since": 2020}}],
        })
        assert edges.status_code == HTTPStatus.OK, edges.text
        await db_pool.execute(
            "INSERT INTO graphos_meta.saved_queries (tenant, name, definition) VALUES "
            "($1, 'titles', $2::jsonb), ($1, 'kinds', $3::jsonb);",
            NAME, json.dumps({"query": "MATCH (d:Docu) RETURN d.title"}),
            json.dumps({"query": "MATCH (d:Docu) RETURN d.kind"}),
        )
        yield client
    await _fresh(db_pool)


async def _rename(api: httpx.AsyncClient, on: str, old: str, new: str, *,
                  token: str = "owner", dry_run: bool = False) -> httpx.Response:
    return await api.post(
        f"/v1/tenants/{NAME}/schema:rename" + ("?dry_run=true" if dry_run else ""),
        json={"kind": "property", "on": on, "from": old, "to": new}, headers=_as(token),
    )


async def _run_job(pool: asyncpg.Pool, job_id: str) -> dict[str, Any]:
    """Run the queued job as the runner would, and record its outcome."""
    row = await pool.fetchrow(
        "UPDATE graphos_meta.jobs SET status = 'running', started_at = now() "
        "WHERE id = $1 RETURNING tenant, params::text AS params;",
        UUID(job_id),
    )
    jobs.CURRENT_JOB.set(UUID(job_id))
    try:
        params = json.loads(row["params"])
        result = await schema_rename.run_property_job(pool, row["tenant"], params)
    finally:
        jobs.CURRENT_JOB.set(None)
    await pool.execute(
        "UPDATE graphos_meta.jobs SET status = 'succeeded', finished_at = now() WHERE id = $1;",
        UUID(job_id),
    )
    return result.model_dump(by_alias=True)


async def _props(pool: asyncpg.Pool, table: str) -> dict[str, dict[str, Any]]:
    rows = await pool.fetch(
        f'SELECT properties::text AS p, graphos_rev AS rev FROM ONLY "{NAME}"."{table}";',
    )
    return {json.loads(r["p"])["id"]: {**json.loads(r["p"]), "_rev": r["rev"]} for r in rows}


@requires_db
@pytest.mark.asyncio
async def test_a_dry_run_reports_rows_parts_and_queries(api: httpx.AsyncClient) -> None:
    resp = await _rename(api, "Docu", "title", "headline", dry_run=True)
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["rows"] == 2 and body["dry_run"] is True  # noqa: PLR2004, PT018
    assert set(body["spec_parts"]) == {
        "property_schema", "display", "deprecated", "property_indexes",
    }
    assert body["saved_queries"] == ["titles"]
    assert registered_tenants()[NAME].renames == ()


@requires_db
@pytest.mark.asyncio
async def test_a_property_is_renamed_on_every_row_by_a_job(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    before = {**await _props(db_pool, "Docu"), **await _props(db_pool, "Report")}
    resp = await _rename(api, "Docu", "title", "headline")
    assert resp.status_code == HTTPStatus.ACCEPTED, resp.text
    job_id = resp.json()["job_id"]
    assert resp.headers["location"] == f"/v1/jobs/{job_id}"
    entry = registered_tenants()[NAME].renames[-1]
    assert (entry["status"], entry["job_id"], entry["on"]) == ("running", job_id, "Docu")

    # While it runs, a write naming the old name succeeds with a warning.
    meanwhile = await api.post(f"/v1/{NAME}/graph/nodes", headers=_as("owner"), json={
        "nodes": [{"id": "d3", "label": "Docu", "properties": {"title": "Late"}}],
    })
    assert meanwhile.status_code == HTTPStatus.OK, meanwhile.text
    assert meanwhile.json()["property_warnings"]["Docu.title:renamed"] == 1

    result = await _run_job(db_pool, job_id)
    assert result["rows"] == 3  # noqa: PLR2004 -- d1, d3 and r1 (the child label)

    after = {**await _props(db_pool, "Docu"), **await _props(db_pool, "Report")}
    assert after["d1"]["headline"] == "Notes" and "title" not in after["d1"]
    assert after["r1"]["headline"] == "Q3" and after["d3"]["headline"] == "Late"
    assert after["d1"]["_rev"] > before["d1"]["_rev"]
    assert after["d2"]["_rev"] == before["d2"]["_rev"]          # no title: not rewritten

    spec = registered_tenants()[NAME]
    assert spec.renames[-1]["status"] == "done"
    assert "headline" in spec.property_schema["vertex"]["Docu"]["properties"]
    assert spec.property_indexes[0]["properties"] == ["headline"]
    assert spec.display["vertex"]["Docu"]["caption"] == "headline"
    assert "Docu" not in spec.deprecated.get("vertex", {})

    indexes = {r["indexname"] for r in await db_pool.fetch(
        "SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND tablename = 'Docu';", NAME,
    )}
    assert "Docu_headline_btree_idx" in indexes and "Docu_title_btree_idx" not in indexes
    catalogue = {(r["label"], r["property"]) for r in await db_pool.fetch(
        "SELECT label, property FROM graphos_meta.node_properties WHERE tenant = $1;", NAME,
    )}
    assert ("Docu", "headline") in catalogue and ("Docu", "title") not in catalogue
    progress = await db_pool.fetchval(
        "SELECT progress FROM graphos_meta.jobs WHERE id = $1;", UUID(job_id),
    )
    assert json.loads(progress)["done"] == 3  # noqa: PLR2004
    records = await db_pool.fetch(
        "SELECT op, detail FROM graphos_meta.changes WHERE tenant = $1 AND "
        "(detail ? 'renamed_property' OR detail ? 'renamed') ORDER BY txid, seq;",
        NAME,
    )
    assert [r["op"] for r in records][-1] == "schema"
    assert {r["op"] for r in records} == {"patch", "schema"}

    # Afterwards, a write naming the old name is refused, in any mode.
    refused = await api.post(f"/v1/{NAME}/graph/nodes", headers=_as("owner"), json={
        "nodes": [{"id": "d4", "label": "Docu", "properties": {"title": "Again"}}],
    })
    assert refused.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, refused.text
    assert "renamed to 'headline'" in refused.text


@requires_db
@pytest.mark.asyncio
async def test_an_edge_property_is_renamed(api: httpx.AsyncClient, db_pool: asyncpg.Pool) -> None:
    resp = await _rename(api, "CITES", "since", "year")
    assert resp.status_code == HTTPStatus.ACCEPTED, resp.text
    await _run_job(db_pool, resp.json()["job_id"])
    props = json.loads(await db_pool.fetchval(
        f'SELECT properties::text FROM "{NAME}"."CITES";',
    ))
    assert props == {"year": 2020}


@requires_db
@pytest.mark.asyncio
async def test_what_a_property_rename_refuses(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    await api.post(f"/v1/{NAME}/graph/nodes", headers=_as("owner"), json={
        "nodes": [{"id": "d9", "label": "Docu", "properties": {"title": "a", "headline": "b"}}],
    })
    for on, old, new, needle in (
        ("Docu", "title", "headline", "have both"),
        ("Docu", "id", "key", "graphos needs it"),
        ("Docu", "kind", "graphos_kind", "graphos needs it"),
        ("Tag", "name", "label", "resolve rules of TAGGED"),
        ("Nope", "a", "b", "not a vertex label or edge type"),
        ("Docu", "kind", "kind", "the old one"),
    ):
        resp = await _rename(api, on, old, new)
        assert resp.status_code == HTTPStatus.BAD_REQUEST, (on, old, resp.text)
        assert needle in resp.text, resp.text
    forbidden = await _rename(api, "Docu", "kind", "category", token="maintainer")
    assert forbidden.status_code == HTTPStatus.FORBIDDEN
    first = await _rename(api, "Docu", "kind", "category")
    assert first.status_code == HTTPStatus.ACCEPTED, first.text
    second = await _rename(api, "Report", "kind", "sort")
    assert second.status_code == HTTPStatus.CONFLICT, second.text
    assert await db_pool.fetchval(
        "SELECT count(*) FROM graphos_meta.jobs WHERE tenant = $1 AND kind = 'rename_property';",
        NAME,
    ) == 1


@requires_db
@pytest.mark.asyncio
async def test_a_failed_rename_resumes_when_asked_again(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    first = await _rename(api, "Docu", "kind", "category")
    assert first.status_code == HTTPStatus.ACCEPTED, first.text
    await db_pool.execute(
        "UPDATE graphos_meta.jobs SET status = 'failed', finished_at = now() WHERE id = $1;",
        UUID(first.json()["job_id"]),
    )
    await schema_rename._mark(db_pool, NAME, "Docu", "kind", "category", "failed")  # noqa: SLF001
    assert registered_tenants()[NAME].renames[-1]["status"] == "failed"
    again = await _rename(api, "Docu", "kind", "category")
    assert again.status_code == HTTPStatus.ACCEPTED, again.text
    renames = registered_tenants()[NAME].renames
    assert len(renames) == 1 and renames[0]["status"] == "running"  # noqa: PT018
    await _run_job(db_pool, again.json()["job_id"])
    assert registered_tenants()[NAME].renames[0]["status"] == "done"
    assert "category" in (await _props(db_pool, "Docu"))["d1"]

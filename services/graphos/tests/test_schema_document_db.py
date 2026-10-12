"""DB tests: schema export, import and the dry run (``graphos.schema_document``).

An exported schema imported into a fresh tenant exports again identically; a
dry run answers exactly what the real import then records, and writes
nothing; its warnings name what applying would cost or fail on.
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
from graphos.auth import StaticAuthenticator
from graphos.bootstrap import forget, load_spec
from graphos.config import Settings
from graphos.identity import Principal
from graphos.schema_changes import diff

from .conftest import requires_db

SOURCE, COPY = "graphos_test_schema_src", "graphos_test_schema_copy"
FULL: dict[str, Any] = {
    "name": SOURCE,
    "vertex_labels": ["Person", "Employee", "Doc"],
    "edge_labels": [
        "AUTHORED",
        {"name": "TAGGED", "resolve_rule": {"src_has_array": "tags", "dst_value_from": "name"}},
    ],
    "label_hierarchy": {"Employee": "Person"},
    "edge_endpoints": {"AUTHORED": [["Person", "Doc"]], "TAGGED": [["Doc", "Doc"]]},
    "strict_endpoints": True,
    "search_indexes": {"names": {"properties": ["name"]}},
    "property_indexes": [{"label": "Doc", "properties": ["kind"]}],
    "generated_columns": [{"label": "Doc", "column": "kind", "property": "kind"}],
    "meta": {"owner": "team-a"},
}


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for name in (SOURCE, COPY):
            if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", name):
                await conn.execute(f"SELECT drop_graph('{name}', true);")
            await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", name)
            await conn.execute("DELETE FROM graphos_meta.edge_endpoints WHERE tenant = $1;", name)
            await conn.execute("DELETE FROM graphos_meta.tenant_settings WHERE tenant = $1;", name)
            forget(name)


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _fresh(db_pool)
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        yield client
    await _fresh(db_pool)


async def _export(api: httpx.AsyncClient, name: str) -> dict[str, Any]:
    resp = await api.get(f"/v1/tenants/{name}/spec")
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.headers["content-disposition"] == f'attachment; filename="{name}.schema.json"'
    return json.loads(resp.text)


def _without_origin(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in ("_export", "name")}


@requires_db
@pytest.mark.asyncio
async def test_an_exported_schema_imports_into_a_new_tenant_and_exports_the_same(
    api: httpx.AsyncClient,
) -> None:
    assert (await api.post("/v1/tenants", json=FULL)).status_code == HTTPStatus.OK
    doc = await _export(api, SOURCE)
    assert doc["_export"]["format"] == "graphos.schema" and doc["_export"]["tenant"] == SOURCE
    assert doc["edge_labels"][-1]["resolve_rule"] == FULL["edge_labels"][1]["resolve_rule"]
    assert doc["strict_endpoints"] is True and doc["meta"] == {"owner": "team-a"}

    copy = {**doc, "name": COPY}
    resp = await api.post("/v1/tenants", json=copy)            # the file itself, renamed
    assert resp.status_code == HTTPStatus.OK, resp.text
    again = await _export(api, COPY)
    assert _without_origin(again) == _without_origin(doc)
    assert again["_export"]["tenant"] == COPY

    # Exporting twice gives the same document, apart from when.
    later = await _export(api, SOURCE)
    assert {**later, "_export": None} == {**doc, "_export": None}


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize("replace", [False, True])
async def test_a_dry_run_answers_what_the_import_then_records_and_writes_nothing(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool, replace: bool,
) -> None:
    assert (await api.post("/v1/tenants", json=FULL)).status_code == HTTPStatus.OK
    incoming = {
        "name": SOURCE,
        "vertex_labels": ["Doc", "Invoice"],
        "edge_labels": ["AUTHORED"],
        "property_indexes": [{"label": "Invoice", "properties": ["number"], "unique": True}],
        "meta": {"owner": "team-b"},
    }
    query = "?dry_run=true" + ("&force_shrink=true" if replace else "")
    before_row = await db_pool.fetchval(
        "SELECT spec::text FROM graphos_meta.tenants WHERE name = $1;", SOURCE,
    )
    before_changes = await db_pool.fetchval(
        "SELECT count(*) FROM graphos_meta.changes WHERE tenant = $1;", SOURCE,
    )
    dry = await api.post(f"/v1/tenants{query}", json=incoming)
    assert dry.status_code == HTTPStatus.OK, dry.text
    preview = dry.json()
    assert (preview["status"], preview["exists"]) == ("dry_run", True)
    assert preview["mode"] == ("replace" if replace else "merge")

    # Nothing written: spec, change feed, tables.
    assert await db_pool.fetchval(
        "SELECT spec::text FROM graphos_meta.tenants WHERE name = $1;", SOURCE,
    ) == before_row
    assert await db_pool.fetchval(
        "SELECT count(*) FROM graphos_meta.changes WHERE tenant = $1;", SOURCE,
    ) == before_changes
    assert await db_pool.fetchval("SELECT to_regclass($1);", f'"{SOURCE}"."Invoice"') is None

    # The real import changes exactly what the dry run said.
    previous = await load_spec(db_pool, SOURCE)
    real = await api.post("/v1/tenants" + ("?force_shrink=true" if replace else ""), json=incoming)
    assert real.status_code == HTTPStatus.OK, real.text
    assert diff(previous, await load_spec(db_pool, SOURCE)) == preview["changes"]
    if replace:
        assert set(preview["changes"]["removed"]["vertex_labels"]) == {"Employee", "Person"}
        assert any("keep their tables and data" in w for w in preview["warnings"])
    else:
        assert preview["changes"]["added"]["vertex_labels"] == ["Invoice"]


@requires_db
@pytest.mark.asyncio
async def test_a_dry_run_warns_about_rewrites_and_repeated_values(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    assert (await api.post("/v1/tenants", json=FULL)).status_code == HTTPStatus.OK
    nodes = [{"id": f"d{i}", "label": "Doc", "properties": {"owner": "u1"}} for i in (1, 2)]
    assert (await api.post(f"/v1/{SOURCE}/graph/nodes", json={"nodes": nodes})).status_code == 200  # noqa: PLR2004
    dry = await api.post("/v1/tenants?dry_run=true", json={
        "name": SOURCE,
        "generated_columns": [{"label": "Doc", "column": "owner", "property": "owner"}],
        "property_indexes": [{"label": "Doc", "properties": ["owner"], "unique": True}],
    })
    assert dry.status_code == HTTPStatus.OK, dry.text
    warnings = " | ".join(dry.json()["warnings"])
    assert "Doc.owner is added to a label with about" in warnings
    assert "nodes already share these values" in warnings


@requires_db
@pytest.mark.asyncio
async def test_a_dry_run_refuses_what_the_import_would(api: httpx.AsyncClient) -> None:
    assert (await api.post("/v1/tenants", json=FULL)).status_code == HTTPStatus.OK
    for body, needle in (
        ({"label_hierarchy": {"Nope": "Person"}}, "not in vertex_labels"),
        ({"search_indexes": {"t": {"labels": ["Nope"], "properties": ["x"]}}},
         "not a vertex label"),
        ({"search_indexes": {"t": {"properties": ["x"], "analyzer": "text",
                                   "language": "klingon"}}}, "unknown text-search language"),
        ({"generated_columns": [{"label": "Doc", "column": "kind", "property": "kind",
                                 "type": "jsonb"}]}, "cannot change"),
        ({"_export": {"format": "graphos.schema", "version": 2}}, "version 2"),
    ):
        resp = await api.post("/v1/tenants?dry_run=true", json={"name": SOURCE, **body})
        assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, (body, resp.text)
        assert needle in resp.text, resp.text


@requires_db
@pytest.mark.asyncio
async def test_a_dry_run_for_a_new_tenant_and_the_levels(db_pool: asyncpg.Pool) -> None:
    await _fresh(db_pool)
    app = create_app(Settings(db_password="placeholder"))
    app.state.pool = db_pool
    app.state.authenticator = StaticAuthenticator({
        "tok_read": Principal.static("reader", tenants={SOURCE: "read"}),
        "tok_maintain": Principal.static("keeper", tenants={SOURCE: "maintain"}),
        "tok_create": Principal.static("svc", create_tenants=["graphos_test_schema_*"]),
    })

    def auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as api:
            dry = await api.post("/v1/tenants?dry_run=true", json=FULL, headers=auth("tok_create"))
            assert dry.status_code == HTTPStatus.OK, dry.text
            assert dry.json()["exists"] is False and dry.json()["changes"]["registered"] is True
            assert await load_spec(db_pool, SOURCE) is None              # still nothing
            assert (await api.post("/v1/tenants?dry_run=true", json=FULL,
                                   headers=auth("tok_read"))).status_code == HTTPStatus.FORBIDDEN

            assert (await api.post("/v1/tenants", json=FULL,
                                   headers=auth("tok_create"))).status_code == HTTPStatus.OK
            path = f"/v1/tenants/{SOURCE}/spec"
            read = await api.get(path, headers=auth("tok_read"))
            assert read.status_code == HTTPStatus.FORBIDDEN
            assert (await api.get(path, headers=auth("tok_maintain"))).status_code == HTTPStatus.OK
            missing = await api.get("/v1/tenants/graphos_test_schema_none/spec",
                                    headers=auth("tok_create"))
            assert missing.status_code == HTTPStatus.NOT_FOUND
    finally:
        await _fresh(db_pool)

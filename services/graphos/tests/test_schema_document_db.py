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
    "display": {
        "vertex": {"Person": {"color": "#5FA584", "caption": "full_name", "icon": "person"}},
        "edge": {"AUTHORED": {"color": "#8D97AE"}},
    },
}


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for name in (SOURCE, COPY):
            if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", name):
                await conn.execute(f"SELECT drop_graph('{name}', true);")
            await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", name)
            await conn.execute(
                "DELETE FROM graphos_meta.unregistered_tenants WHERE name = $1;", name,
            )
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
async def test_a_dry_run_lists_what_stops_working_with_the_rows_it_concerns(
    api: httpx.AsyncClient,
) -> None:
    strict = {"mode": "strict", "vertex": {"Doc": {"properties": {"owner": {"type": "string"}}}}}
    registered = await api.post("/v1/tenants", json={**FULL, "property_schema": strict})
    assert registered.status_code == HTTPStatus.OK, registered.text
    nodes = [{"id": f"d{i}", "label": "Doc", "properties": {"owner": "u1"}} for i in (1, 2)]
    assert (await api.post(f"/v1/{SOURCE}/graph/nodes", json={"nodes": nodes})).status_code == 200  # noqa: PLR2004
    tighter = {"mode": "strict", "vertex": {"Doc": {"properties": {
        "owner": {"type": "string", "required": True}}}}}
    dry = await api.post("/v1/tenants?dry_run=true&force_shrink=true", json={
        **FULL, "property_schema": tighter, "search_indexes": {},
    })
    assert dry.status_code == HTTPStatus.OK, dry.text
    breaking = {(b["name"], b["change"]): b for b in dry.json()["breaking"]}
    assert set(breaking) == {("Doc", "property_required"), (None, "search_index_removed")}
    assert breaking[("Doc", "property_required")]["rows"] >= 1
    assert breaking[(None, "search_index_removed")]["rows"] is None
    relaxed = await api.post("/v1/tenants?dry_run=true", json={
        "name": SOURCE, "vertex_labels": ["Invoice"],
    })
    assert relaxed.json()["breaking"] == []


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
        ({"_export": {"format": "graphos.schema", "version": 4}}, "version 4"),
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


@requires_db
@pytest.mark.asyncio
async def test_a_null_meta_key_is_removed_and_the_dry_run_says_so(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    body = {"name": SOURCE, "vertex_labels": ["Doc"], "meta": {"owner": "team-a", "model": "v1"}}
    assert (await api.post("/v1/tenants", json=body)).status_code == HTTPStatus.OK
    change = {"name": SOURCE, "meta": {"model": None, "basis": "a note"}}
    dry = await api.post("/v1/tenants?dry_run=true", json=change)
    assert dry.json()["changes"] == {"added": {"meta": ["basis"]}, "removed": {"meta": ["model"]}}

    assert (await api.post("/v1/tenants", json=change)).status_code == HTTPStatus.OK
    spec = await load_spec(db_pool, SOURCE)
    assert spec is not None and spec.meta == {"owner": "team-a", "basis": "a note"}
    assert spec.vertex_labels == ("Doc",)       # the rest of the schema is untouched
    # Recorded in the feed by key (the text stays in the schema).
    detail = await db_pool.fetchval(
        "SELECT detail::text FROM graphos_meta.changes WHERE tenant = $1 "
        "ORDER BY seq DESC LIMIT 1;",
        SOURCE,
    )
    assert json.loads(detail) == {"added": {"meta": ["basis"]}, "removed": {"meta": ["model"]}}
    # Removing a key that is not there changes nothing.
    again = await api.post(
        "/v1/tenants?dry_run=true", json={"name": SOURCE, "meta": {"model": None}},
    )
    assert again.json()["changes"] is None


@requires_db
@pytest.mark.asyncio
async def test_display_merges_by_entry_and_null_removes_one(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    body = {
        "name": SOURCE, "vertex_labels": ["Person", "Doc"], "edge_labels": ["AUTHORED"],
        "display": {"vertex": {"Person": {"color": "#5fa584", "caption": "full_name"},
                               "Doc": {"icon": "document"}},
                    "edge": {"AUTHORED": {"color": "#8D97AE"}}},
    }
    assert (await api.post("/v1/tenants", json=body)).status_code == HTTPStatus.OK
    # A registration that leaves display out keeps it (the merge SQL).
    assert (await api.post("/v1/tenants", json={"name": SOURCE})).status_code == HTTPStatus.OK
    schema = (await api.get(f"/v1/{SOURCE}/schema")).json()
    # Keys left out of an entry are answered as null.
    shown = {part: {name: {k: v for k, v in entry.items() if v is not None}
                    for name, entry in entries.items()}
             for part, entries in schema["display"].items()}
    assert shown == {
        "vertex": {"Person": {"color": "#5FA584", "caption": "full_name"},
                   "Doc": {"icon": "document"}},
        "edge": {"AUTHORED": {"color": "#8D97AE"}},
    }
    # An entry declared again replaces the stored one; null removes one.
    change = {"name": SOURCE, "display": {"vertex": {"Person": {"color": "#C9825B"}, "Doc": None},
                                          "edge": {"AUTHORED": None}}}
    dry = await api.post("/v1/tenants?dry_run=true", json=change)
    assert dry.json()["changes"] == {
        "removed": {"display": ["edge:AUTHORED", "vertex:Doc"]},
        "changed": {"display": ["vertex:Person"]},
    }
    assert (await api.post("/v1/tenants", json=change)).status_code == HTTPStatus.OK
    spec = await load_spec(db_pool, SOURCE)
    assert spec is not None
    assert spec.display == {"vertex": {"Person": {"color": "#C9825B"}}}
    # Entries must name the tenant's labels and types.
    bad = await api.post(
        "/v1/tenants", json={"name": SOURCE, "display": {"edge": {"CITES": {"color": "#8D97AE"}}}},
    )
    assert bad.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "'CITES' is not an edge label" in bad.text
    # A colour lost on a canvas is a warning, not a refusal.
    faint = await api.post(
        "/v1/tenants?dry_run=true",
        json={"name": SOURCE, "display": {"vertex": {"Doc": {"color": "#E0E2E6"}}}},
    )
    assert faint.json()["warnings"] == [
        "display colour #E0E2E6 of 'Doc' is hard to see on the light canvas",
    ]


PROPERTIES: dict[str, Any] = {
    "mode": "warn",
    "vertex": {
        "*": {"properties": {"source": {"type": "string", "required": True}}},
        "Person": {"description": "Someone who writes", "undeclared": "warn", "properties": {
            "name": {"type": "string", "required": True},
        }},
        "Employee": {"properties": {"name": {"pattern": "[A-Z].*"}}},
    },
    "edge": {"AUTHORED": {"properties": {"role": {"type": "string", "enum": ["lead", "co"]}}}},
}


@requires_db
@pytest.mark.asyncio
async def test_a_property_schema_is_kept_merged_by_entry_and_exported(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    body = {**FULL, "property_schema": PROPERTIES}
    resp = await api.post("/v1/tenants", json=body)
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json()["n_property_definitions"] == 4
    assert resp.json()["property_mode"] == "warn"
    # A registration that leaves it out keeps it (the persist_spec SQL).
    assert (await api.post("/v1/tenants", json={"name": SOURCE})).status_code == HTTPStatus.OK
    schema = (await api.get(f"/v1/{SOURCE}/schema")).json()["property_schema"]
    assert schema["mode"] == "warn"
    assert schema["vertex"]["Person"]["description"] == "Someone who writes"
    assert schema["vertex"]["Employee"]["properties"]["name"]["pattern"] == "[A-Z].*"

    # An entry declared again replaces the stored one; null removes one; mode kept.
    change = {"name": SOURCE, "property_schema": {
        "vertex": {"Employee": None}, "edge": {"AUTHORED": {"description": "Who wrote what"}},
    }}
    dry = await api.post("/v1/tenants?dry_run=true", json=change)
    assert dry.json()["changes"] == {
        "removed": {"property_schema": ["vertex:Employee"]},
        "changed": {"property_schema": ["edge:AUTHORED"]},
    }
    assert (await api.post("/v1/tenants", json=change)).status_code == HTTPStatus.OK
    spec = await load_spec(db_pool, SOURCE)
    assert spec is not None
    assert set(spec.property_schema["vertex"]) == {"*", "Person"}
    assert spec.property_schema["edge"] == {"AUTHORED": {"description": "Who wrote what"}}
    assert spec.property_schema["mode"] == "warn"

    # Exported as a version 2 file, imported into another tenant identically.
    doc = await _export(api, SOURCE)
    assert doc["_export"]["version"] == 2
    copy = {**doc, "name": COPY}
    assert (await api.post("/v1/tenants", json=copy)).status_code == HTTPStatus.OK
    assert _without_origin(await _export(api, COPY)) == _without_origin(doc)


@requires_db
@pytest.mark.asyncio
async def test_a_property_schema_must_fit_the_tenant(api: httpx.AsyncClient) -> None:
    assert (await api.post("/v1/tenants", json=FULL)).status_code == HTTPStatus.OK
    unknown = {"name": SOURCE, "property_schema": {"vertex": {"Invoice": {}}}}
    resp = await api.post("/v1/tenants", json=unknown)
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "'Invoice' is not a vertex label" in resp.text
    # Employee inherits from Person: it may narrow name, not change its type.
    retyped = {"name": SOURCE, "property_schema": {"vertex": {
        "Person": {"properties": {"name": {"type": "string"}}},
        "Employee": {"properties": {"name": {"type": "integer"}}},
    }}}
    resp = await api.post("/v1/tenants", json=retyped)
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "not change its type" in resp.text

"""DB tests: renaming a vertex label or an edge type in place (ADR-0009 §2).

The rows, their graph ids, the edges and inheritance stay; everything graphos
keeps that names the label or type follows it; the old name answers 409 with
where it went, on writes and on registration; saved queries naming it are
reported; a dry run writes nothing.
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
from graphos.bootstrap import forget, registered_tenants
from graphos.config import Settings
from graphos.identity import Principal

from .conftest import requires_db

NAME = "graphos_test_rename"
SCHEMA: dict[str, Any] = {
    "name": NAME,
    "vertex_labels": ["Person", "Employee", "Docu", "Report"],
    "edge_labels": ["WROTE", "CITES"],
    "label_hierarchy": {"Report": "Docu"},
    "edge_endpoints": {"WROTE": [{"src": "Person", "dst": "Docu"}]},
    "property_indexes": [{"label": "Docu", "properties": ["kind"]}],
    "search_indexes": {"titles": {"labels": ["Docu"], "properties": ["title"]}},
    "display": {"vertex": {"Docu": {"color": "#336699"}}, "edge": {"CITES": {"color": "#993366"}}},
    "property_schema": {"vertex": {"Docu": {"properties": {"title": {"type": "string"}}}}},
    "deprecated": {"vertex": {"Report": {"deprecated": True, "replaced_by": "Docu"}}},
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
        nodes = [
            {"id": "p1", "label": "Person", "properties": {"name": "Ada"}},
            {"id": "d1", "label": "Docu", "properties": {"title": "Notes", "kind": "memo"}},
            {"id": "r1", "label": "Report", "properties": {"title": "Q3"}},
        ]
        for node in nodes:
            wrote = await client.post(
                f"/v1/{NAME}/graph/nodes", json={"nodes": [node]}, headers=_as("owner"),
            )
            assert wrote.status_code == HTTPStatus.OK, wrote.text
        edges = await client.post(f"/v1/{NAME}/graph/edges", headers=_as("owner"), json={"edges": [
            {"src_id": "p1", "src_label": "Person", "dst_id": "d1", "dst_label": "Docu",
             "type": "WROTE"},
            {"src_id": "r1", "src_label": "Report", "dst_id": "d1", "dst_label": "Docu",
             "type": "CITES"},
        ]})
        assert edges.status_code == HTTPStatus.OK, edges.text
        await db_pool.execute(
            "INSERT INTO graphos_meta.saved_queries (tenant, name, definition) VALUES "
            "($1, 'docs', $2::jsonb), ($1, 'people', $3::jsonb);",
            NAME, json.dumps({"query": "MATCH (d:Docu) RETURN d"}),
            json.dumps({"query": "MATCH (p:Person) RETURN p"}),
        )
        yield client
    await _fresh(db_pool)


async def _rename(api: httpx.AsyncClient, kind: str, old: str, new: str, *,
                  token: str = "owner", dry_run: bool = False) -> httpx.Response:
    return await api.post(
        f"/v1/tenants/{NAME}/schema:rename" + ("?dry_run=true" if dry_run else ""),
        json={"kind": kind, "from": old, "to": new}, headers=_as(token),
    )


async def _graph_ids(pool: asyncpg.Pool, label: str) -> list[str]:
    rows = await pool.fetch(f'SELECT id::text AS id FROM ONLY "{NAME}"."{label}" ORDER BY 1;')
    return [r["id"] for r in rows]


async def _indexes(pool: asyncpg.Pool, table: str) -> list[str]:
    rows = await pool.fetch(
        "SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND tablename = $2 ORDER BY 1;",
        NAME, table,
    )
    return [r["indexname"] for r in rows]


@requires_db
@pytest.mark.asyncio
async def test_a_dry_run_reports_and_writes_nothing(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    resp = await _rename(api, "label", "Docu", "Document", dry_run=True)
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["dry_run"] is True and body["from"] == "Docu"
    assert set(body["spec_parts"]) >= {
        "label_hierarchy", "property_indexes", "search_indexes", "display", "property_schema",
    }
    assert body["saved_queries"] == ["docs"]
    assert body["endpoints"] == 1 and body["rows"] == 1  # noqa: PT018
    assert ["Docu_properties_gin", "Document_properties_gin"] in body["indexes"]
    assert "Docu" in registered_tenants()[NAME].vertex_labels
    assert await _graph_ids(db_pool, "Docu")


@requires_db
@pytest.mark.asyncio
async def test_a_label_is_renamed_in_place(api: httpx.AsyncClient, db_pool: asyncpg.Pool) -> None:
    before_ids = await _graph_ids(db_pool, "Docu")
    resp = await _rename(api, "label", "Docu", "Document")
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json()["saved_queries"] == ["docs"]

    # Rows, graph ids and edges stay; the child still inherits from the renamed parent.
    assert await _graph_ids(db_pool, "Document") == before_ids
    node = await api.get(f"/v1/{NAME}/graph/nodes/d1?label=Document", headers=_as("owner"))
    assert node.status_code == HTTPStatus.OK, node.text
    near = await api.get(f"/v1/{NAME}/graph/neighbors?node_id=p1", headers=_as("owner"))
    assert [n["neighbor_label"] for n in near.json()["neighbors"]] == ["Document"]
    inherited = await db_pool.fetchval(f'SELECT count(*) FROM "{NAME}"."Document";')
    assert inherited == 2  # noqa: PLR2004 -- d1, and r1 through the Report child

    # Everything that named it follows.
    spec = registered_tenants()[NAME]
    assert "Document" in spec.vertex_labels and "Docu" not in spec.vertex_labels
    assert ("Report", "Document") in spec.label_hierarchy
    assert spec.property_indexes[0]["label"] == "Document"
    assert spec.search_indexes["titles"]["labels"] == ["Document"]
    assert "Document" in spec.display["vertex"] and "Document" in spec.property_schema["vertex"]
    assert spec.deprecated["vertex"]["Report"]["replaced_by"] == "Document"
    assert spec.edge_endpoints == (("WROTE", "Person", "Document"),)
    assert spec.renames[-1]["kind"] == "label" and spec.renames[-1]["by"] == "olive"
    assert await db_pool.fetchval(
        "SELECT count(*) FROM graphos_meta.node_properties WHERE tenant = $1 AND label = 'Docu';",
        NAME,
    ) == 0

    # graphos's indexes carry the new name, and registering again builds no second set.
    names = await _indexes(db_pool, "Document")
    assert "Document_properties_gin" in names and not [n for n in names if "Docu_" in n]
    again = await api.post("/v1/tenants", json={"name": NAME}, headers=_as("owner"))
    assert again.status_code == HTTPStatus.OK, again.text
    assert await _indexes(db_pool, "Document") == names

    # Cypher sees it under the new name only, on another connection.
    async with db_pool.acquire() as conn:
        await conn.execute("LOAD 'age'; SET search_path = ag_catalog, \"$user\", public;")
        new = await conn.fetchval(
            f"SELECT count(*) FROM cypher('{NAME}', $$ MATCH (n:Document) RETURN n $$) "
            "AS (n agtype);",
        )
        old = await conn.fetchval(
            f"SELECT count(*) FROM cypher('{NAME}', $$ MATCH (n:Docu) RETURN n $$) AS (n agtype);",
        )
    assert (new, old) == (2, 0)

    # One schema record.
    detail = await db_pool.fetchval(
        "SELECT detail FROM graphos_meta.changes WHERE tenant = $1 AND op = 'schema' "
        "ORDER BY txid DESC, seq DESC LIMIT 1;",
        NAME,
    )
    assert json.loads(detail)["renamed"] == {"kind": "label", "from": "Docu", "to": "Document"}


@requires_db
@pytest.mark.asyncio
async def test_the_old_name_answers_where_it_went(api: httpx.AsyncClient) -> None:
    assert (await _rename(api, "label", "Docu", "Document")).status_code == HTTPStatus.OK
    write = await api.post(f"/v1/{NAME}/graph/nodes", headers=_as("owner"), json={
        "nodes": [{"id": "d2", "label": "Docu", "properties": {}}],
    })
    assert write.status_code == HTTPStatus.CONFLICT
    assert "'Docu' was renamed to 'Document'" in write.text
    register = await api.post(
        "/v1/tenants", json={"name": NAME, "vertex_labels": ["Docu"]}, headers=_as("owner"),
    )
    assert register.status_code == HTTPStatus.CONFLICT, register.text


@requires_db
@pytest.mark.asyncio
async def test_an_edge_type_is_renamed_with_its_pairs_and_styles(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    resp = await _rename(api, "edge_type", "WROTE", "AUTHORED")
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json()["endpoints"] == 1
    spec = registered_tenants()[NAME]
    assert spec.edge_endpoints == (("AUTHORED", "Person", "Docu"),)
    assert await db_pool.fetchval(f'SELECT count(*) FROM "{NAME}"."AUTHORED";') == 1
    near = await api.get(f"/v1/{NAME}/graph/neighbors?node_id=p1", headers=_as("owner"))
    assert [n["edge_type"] for n in near.json()["neighbors"]] == ["AUTHORED"]
    old = await api.post(f"/v1/{NAME}/graph/edges", headers=_as("owner"), json={"edges": [
        {"src_id": "p1", "src_label": "Person", "dst_id": "d1", "dst_label": "Docu",
         "type": "WROTE"},
    ]})
    assert old.status_code == HTTPStatus.CONFLICT, old.text
    styled = await _rename(api, "edge_type", "CITES", "REFERS_TO")
    assert styled.status_code == HTTPStatus.OK, styled.text
    assert "REFERS_TO" in registered_tenants()[NAME].display["edge"]


@requires_db
@pytest.mark.asyncio
async def test_what_a_rename_refuses(api: httpx.AsyncClient, db_pool: asyncpg.Pool) -> None:
    for kind, old, new in (
        ("label", "Docu", "Person"),          # taken
        ("label", "Docu", "WROTE"),           # taken by an edge type
        ("label", "Docu", "document"),        # not a label name
        ("label", "Docu", "Docu"),            # the same
        ("label", "Nope", "Other"),           # not a label
        ("edge_type", "Docu", "X"),           # a label, not a type
    ):
        resp = await _rename(api, kind, old, new)
        assert resp.status_code == HTTPStatus.BAD_REQUEST, (kind, old, new, resp.text)
    forbidden = await _rename(api, "label", "Docu", "Document", token="maintainer")
    assert forbidden.status_code == HTTPStatus.FORBIDDEN
    await db_pool.execute(
        "INSERT INTO graphos_meta.jobs (id, tenant, kind, status, params) "
        "VALUES (gen_random_uuid(), $1, 'vacuum', 'queued', '{}'::jsonb);",
        NAME,
    )
    busy = await _rename(api, "label", "Docu", "Document")
    assert busy.status_code == HTTPStatus.CONFLICT, busy.text
    assert "Docu" in registered_tenants()[NAME].vertex_labels

"""DB tests: dropping a deprecated label or edge type that has no rows (ADR-0009 §1).

The AGE label goes with its indexes, the spec and ``graphos_meta`` forget it,
one ``schema`` record; one with rows, or not deprecated, is refused.
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

NAME = "graphos_test_drop"
SCHEMA: dict[str, Any] = {
    "name": NAME,
    "vertex_labels": ["Person", "Legacy", "Old"],
    "edge_labels": ["KNOWS", {"name": "LINKS", "resolve_rule": {
        "src_has_array": "tags", "dst_value_from": "name"}}],
    "edge_endpoints": {"KNOWS": [{"src": "Person", "dst": "Legacy"}],
                       "LINKS": [{"src": "Person", "dst": "Person"}]},
    "property_indexes": [{"label": "Legacy", "properties": ["kind"]}],
    "search_indexes": {"legacy_only": {"labels": ["Legacy"], "properties": ["title"]},
                       "both": {"labels": ["Person", "Legacy"], "properties": ["name"]}},
    "display": {"vertex": {"Legacy": {"color": "#336699"}},
                "edge": {"LINKS": {"color": "#993366"}}},
    "deprecated": {"vertex": {"Legacy": {"deprecated": True, "replaced_by": "Person"},
                              "Old": {"deprecated": True}},
                   "edge": {"LINKS": {"deprecated": True}}},
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
            "nodes": [{"id": "o1", "label": "Old", "properties": {"name": "kept"}}],
        })
        assert wrote.status_code == HTTPStatus.OK, wrote.text
        await db_pool.execute(
            "INSERT INTO graphos_meta.saved_queries (tenant, name, definition) VALUES "
            "($1, 'legacy', $2::jsonb);",
            NAME, json.dumps({"query": "MATCH (l:Legacy) RETURN l"}),
        )
        yield client
    await _fresh(db_pool)


async def _drop(api: httpx.AsyncClient, label: str, token: str = "owner") -> httpx.Response:
    return await api.delete(f"/v1/tenants/{NAME}/labels/{label}", headers=_as(token))


async def _labels(pool: asyncpg.Pool) -> set[str]:
    rows = await pool.fetch(
        "SELECT l.name FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph g "
        "ON g.graphid = l.graph WHERE g.name = $1;", NAME,
    )
    return {r["name"] for r in rows}


@requires_db
@pytest.mark.asyncio
async def test_an_empty_deprecated_label_is_dropped(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    resp = await _drop(api, "Legacy")
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json() == {"tenant": NAME, "kind": "label", "name": "Legacy",
                           "saved_queries": ["legacy"]}
    assert "Legacy" not in await _labels(db_pool)
    assert not await db_pool.fetchval("SELECT to_regclass($1);", f'"{NAME}"."Legacy"')
    spec = registered_tenants()[NAME]
    assert "Legacy" not in spec.vertex_labels
    assert set(spec.search_indexes) == {"both"}
    assert spec.search_indexes["both"]["labels"] == ["Person"]
    assert spec.property_indexes == ()
    assert ("KNOWS", "Person", "Legacy") not in spec.edge_endpoints
    assert "Legacy" not in spec.deprecated.get("vertex", {})
    pairs = await db_pool.fetchval(
        "SELECT count(*) FROM graphos_meta.edge_endpoints WHERE tenant = $1 AND dst_label = $2;",
        NAME, "Legacy",
    )
    assert pairs == 0
    record = await db_pool.fetchval(
        "SELECT detail::text FROM graphos_meta.changes WHERE tenant = $1 AND op = 'schema' "
        "ORDER BY seq DESC LIMIT 1;", NAME,
    )
    assert json.loads(record) == {"dropped": {"kind": "label", "name": "Legacy"}}
    info = await api.get(f"/v1/tenants/{NAME}", headers=_as("owner"))
    assert "Legacy" not in info.json()["vertex_labels"]
    again = await api.post("/v1/tenants", headers=_as("creator"), json={
        "name": NAME, "vertex_labels": ["Legacy"],
    })
    assert again.status_code == HTTPStatus.OK, again.text
    assert "Legacy" in await _labels(db_pool)


@requires_db
@pytest.mark.asyncio
async def test_an_empty_deprecated_edge_type_is_dropped(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    resp = await _drop(api, "LINKS")
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json()["kind"] == "edge_type"
    assert "LINKS" not in await _labels(db_pool)
    spec = registered_tenants()[NAME]
    assert spec.edge_labels == ("KNOWS",)
    assert spec.resolve_rules == ()
    assert all(t != "LINKS" for t, _, _ in spec.edge_endpoints)
    assert "edge" not in spec.display


@requires_db
@pytest.mark.asyncio
async def test_what_a_drop_refuses(api: httpx.AsyncClient, db_pool: asyncpg.Pool) -> None:
    has_rows = await _drop(api, "Old")
    assert has_rows.status_code == HTTPStatus.BAD_REQUEST
    assert "has 1 row(s)" in has_rows.json()["detail"]
    in_use = await _drop(api, "Person")
    assert in_use.status_code == HTTPStatus.BAD_REQUEST
    assert "not deprecated" in in_use.json()["detail"]
    assert (await _drop(api, "Nope")).status_code == HTTPStatus.BAD_REQUEST
    assert (await _drop(api, "Legacy", "maintainer")).status_code == HTTPStatus.FORBIDDEN
    await db_pool.execute(
        "INSERT INTO graphos_meta.jobs (kind, tenant, params) "
        "VALUES ('check_properties', $1, '{}'::jsonb);",
        NAME,
    )
    busy = await _drop(api, "Legacy")
    assert busy.status_code == HTTPStatus.CONFLICT, busy.text
    assert {"Old", "Legacy", "LINKS"} <= await _labels(db_pool)

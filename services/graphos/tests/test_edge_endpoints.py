"""DB tests: declared relationship endpoints (``graphos.endpoints``).

DB-gated.  The API is driven in-process through httpx's ASGI transport with
the test pool: declaring label pairs on registration, how they merge and are
served, how writes are refused under ``strict_endpoints`` (small batches,
bulk, the resolver), removing a pair, and that they live outside the stored
spec so an older server cannot drop them.
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

from graphos import writes
from graphos.app import create_app
from graphos.bootstrap import TENANT_ROWS_SQL, forget
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_endpoints"
SCHEMA: dict[str, Any] = {
    "vertex_labels": ["Repo", "File", "Symbol", "Function", "Class"],
    "edge_labels": [
        "DEFINES",
        "LINKS",
        {"name": "CALLS", "resolve_rule": {
            "src_has_array": "call_sites", "dst_value_from": "name",
        }},
    ],
    "label_hierarchy": {"Function": "Symbol", "Class": "Symbol"},
}


def _app(pool: asyncpg.Pool) -> httpx.AsyncClient:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = pool
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _register(
    api: httpx.AsyncClient, *, force_shrink: bool = False, **body: Any,
) -> httpx.Response:
    params = {"force_shrink": "true"} if force_shrink else None
    return await api.post("/v1/tenants", json={"name": TENANT, **body}, params=params)


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", TENANT):
            await conn.execute(f"SELECT drop_graph('{TENANT}', true);")
        for table in ("tenants", "unregistered_tenants", "edge_endpoints", "tenant_settings"):
            column = "name" if table.endswith("tenants") else "tenant"
            await conn.execute(f"DELETE FROM graphos_meta.{table} WHERE {column} = $1;", TENANT)
    forget(TENANT)


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _fresh(db_pool)
    async with _app(db_pool) as client:
        resp = await _register(client, **SCHEMA, edge_endpoints={
            "DEFINES": [["File", "Symbol"]],
            "CALLS": [["Function", "Function"]],
        })
        assert resp.status_code == HTTPStatus.OK, resp.text
        await writes.upsert_nodes(db_pool, tenant=TENANT, label="Repo", rows=[
            {"id": "r", "props": {}},
        ])
        await writes.upsert_nodes(db_pool, tenant=TENANT, label="File", rows=[
            {"id": "a.py", "props": {"name": "g"}},
        ])
        await writes.upsert_nodes(db_pool, tenant=TENANT, label="Function", rows=[
            {"id": "f", "props": {"name": "f", "call_sites": ["g"]}},
            {"id": "g", "props": {"name": "g"}},
        ])
        await writes.upsert_nodes(db_pool, tenant=TENANT, label="Class", rows=[
            {"id": "C", "props": {"name": "C"}},
        ])
        yield client
    await _fresh(db_pool)


async def _schema(api: httpx.AsyncClient) -> dict[str, Any]:
    resp = await api.get(f"/v1/{TENANT}/schema")
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()


def _pairs(schema: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {(t, p["src"], p["dst"]) for t, ps in schema["edge_endpoints"].items() for p in ps}


def _edge(src: tuple[str, str], dst: tuple[str, str], type_: str) -> dict[str, str]:
    return {
        "src_id": src[0], "src_label": src[1], "dst_id": dst[0], "dst_label": dst[1], "type": type_,
    }


async def _strict(api: httpx.AsyncClient, on: bool = True) -> None:
    resp = await _register(api, strict_endpoints=on)
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json()["strict_endpoints"] is on


# ── declaring, merging, serving ─────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_declared_pairs_are_served_and_kept_out_of_the_stored_spec(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    schema = await _schema(api)
    assert _pairs(schema) == {("DEFINES", "File", "Symbol"), ("CALLS", "Function", "Function")}
    assert schema["strict_endpoints"] is False
    assert schema["rule_edge_labels"] == ["CALLS"]
    async with db_pool.acquire() as conn:
        stored = json.loads(await conn.fetchval(
            "SELECT spec::text FROM graphos_meta.tenants WHERE name = $1;", TENANT,
        ))
    # A server that predates declared endpoints rewrites this JSON on
    # registration; they live in their own table so it cannot drop them.
    assert "edge_endpoints" not in stored and "strict_endpoints" not in stored


@requires_db
@pytest.mark.asyncio
async def test_registrations_add_pairs_and_a_bare_one_keeps_them(api: httpx.AsyncClient) -> None:
    resp = await _register(api, edge_endpoints={"DEFINES": [{"src": "Repo", "dst": "File"}]})
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json()["n_edge_endpoints"] == 3
    # A client that knows nothing of endpoints registers its schema again.
    assert (await _register(api, **SCHEMA)).status_code == HTTPStatus.OK
    assert _pairs(await _schema(api)) == {
        ("DEFINES", "File", "Symbol"),
        ("DEFINES", "Repo", "File"),
        ("CALLS", "Function", "Function"),
    }


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(("pairs", "message"), [
    ({"IMPORTS": [["File", "File"]]}, "not an edge label of the tenant"),
    ({"DEFINES": [["File", "Module"]]}, "not a vertex label of the tenant"),
])
async def test_pairs_must_name_the_tenants_labels(
    api: httpx.AsyncClient, pairs: dict[str, Any], message: str,
) -> None:
    resp = await _register(api, edge_endpoints=pairs)
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert message in resp.text
    # Labels added by the same registration may be used.
    resp = await _register(api, vertex_labels=["Module"], edge_labels=["IMPORTS"],
                           edge_endpoints={"IMPORTS": [["Module", "Module"]]})
    assert resp.status_code == HTTPStatus.OK, resp.text


@requires_db
@pytest.mark.asyncio
async def test_force_shrink_replaces_pairs_only_when_it_states_them(api: httpx.AsyncClient) -> None:
    assert (await _register(api, force_shrink=True, **SCHEMA)).status_code == HTTPStatus.OK
    assert len(_pairs(await _schema(api))) == 2
    resp = await _register(api, force_shrink=True, **SCHEMA,
                           edge_endpoints={"DEFINES": [["Repo", "File"]]})
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert _pairs(await _schema(api)) == {("DEFINES", "Repo", "File")}


@requires_db
@pytest.mark.asyncio
async def test_a_change_to_the_pairs_changes_the_version_replicas_watch(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    async def version() -> str:
        return await db_pool.fetchval(
            f"SELECT version FROM ({TENANT_ROWS_SQL}) r WHERE name = $1;", TENANT,
        )

    before = await version()
    await _register(api, edge_endpoints={"LINKS": [["Repo", "Repo"]]})
    after_pair = await version()
    await _strict(api)
    assert len({before, after_pair, await version()}) == 3


# ── writes under strict_endpoints ───────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_without_strict_every_pair_is_accepted(api: httpx.AsyncClient) -> None:
    resp = await api.post(f"/v1/{TENANT}/graph/edges", json={"edges": [
        _edge(("r", "Repo"), ("C", "Class"), "DEFINES"),
    ]})
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json()["edges_upserted"] == 1


@requires_db
@pytest.mark.asyncio
async def test_strict_refuses_undeclared_pairs_and_honours_inheritance(
    api: httpx.AsyncClient,
) -> None:
    await _strict(api)
    edges = f"/v1/{TENANT}/graph/edges"
    # File -> Class: declared as File -> Symbol, and Class inherits Symbol.
    resp = await api.post(edges, json={"edges": [
        _edge(("a.py", "File"), ("C", "Class"), "DEFINES"),
    ]})
    assert resp.status_code == HTTPStatus.OK, resp.text
    # A type with no declared pairs stays open.
    resp = await api.post(edges, json={"edges": [_edge(("r", "Repo"), ("a.py", "File"), "LINKS")]})
    assert resp.status_code == HTTPStatus.OK, resp.text
    # Repo -> Class is not declared: the whole batch is refused, nothing written.
    resp = await api.post(edges, json={"edges": [
        _edge(("a.py", "File"), ("f", "Function"), "DEFINES"),
        _edge(("r", "Repo"), ("C", "Class"), "DEFINES"),
    ]})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "Repo-[DEFINES]->Class is not declared" in resp.json()["detail"]
    neighbours = await api.get(f"/v1/{TENANT}/graph/neighbors", params={"node_id": "f"})
    assert neighbours.status_code == HTTPStatus.OK, neighbours.text
    assert "a.py" not in neighbours.text


@requires_db
@pytest.mark.asyncio
async def test_strict_bulk_counts_undeclared_lines_invalid(api: httpx.AsyncClient) -> None:
    await _strict(api)
    lines = [
        _edge(("a.py", "File"), ("g", "Function"), "DEFINES"),
        _edge(("r", "Repo"), ("C", "Class"), "DEFINES"),
    ]
    resp = await api.post(
        f"/v1/{TENANT}/graph/edges:bulk",
        content="\n".join(json.dumps(line) for line in lines) + "\n",
        headers={"content-type": "application/x-ndjson"},
    )
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["rows_invalid"] == 1
    assert "line 2:" in body["errors"][0] and "is not declared" in body["errors"][0]
    assert body["edges_upserted"] == 1


@requires_db
@pytest.mark.asyncio
async def test_strict_resolve_links_only_declared_pairs(api: httpx.AsyncClient) -> None:
    # call_sites ["g"] matches the Function g and the File named g.
    open_run = await api.post(f"/v1/{TENANT}/graph/edges:resolve", json={"edge_types": ["CALLS"]})
    assert open_run.status_code == HTTPStatus.OK, open_run.text
    assert open_run.json()["total_edges_written"] == 2
    await _fresh_calls(api)
    await _strict(api)
    strict_run = await api.post(f"/v1/{TENANT}/graph/edges:resolve", json={"edge_types": ["CALLS"]})
    assert strict_run.status_code == HTTPStatus.OK, strict_run.text
    result = strict_run.json()["results"][0]
    assert result["edges_written"] == 1
    assert "not declared for CALLS" in result["warning"]


async def _fresh_calls(api: httpx.AsyncClient) -> None:
    """Delete the CALLS edges the open run wrote."""
    for dst, label in (("g", "Function"), ("a.py", "File")):
        resp = await api.delete(
            f"/v1/{TENANT}/graph/edges/CALLS",
            params={"src": "f", "src_label": "Function", "dst": dst, "dst_label": label},
        )
        assert resp.status_code in (HTTPStatus.OK, HTTPStatus.NO_CONTENT), resp.text


# ── removing a pair, unregistering ──────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_removing_the_last_pair_opens_the_type_again(api: httpx.AsyncClient) -> None:
    await _strict(api)
    path = f"/v1/tenants/{TENANT}/edge_endpoints"
    missing = await api.delete(path, params={"type": "DEFINES", "src": "Repo", "dst": "File"})
    assert missing.status_code == HTTPStatus.NOT_FOUND
    resp = await api.delete(path, params={"type": "DEFINES", "src": "File", "dst": "Symbol"})
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert _pairs(await _schema(api)) == {("CALLS", "Function", "Function")}
    resp = await api.post(f"/v1/{TENANT}/graph/edges", json={"edges": [
        _edge(("r", "Repo"), ("C", "Class"), "DEFINES"),
    ]})
    assert resp.status_code == HTTPStatus.OK, resp.text


@requires_db
@pytest.mark.asyncio
async def test_unregistering_drops_the_pairs_and_settings(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    await _strict(api)
    assert (await api.delete(f"/v1/tenants/{TENANT}")).status_code == HTTPStatus.OK
    async with db_pool.acquire() as conn:
        left = await conn.fetchval(
            "SELECT (SELECT count(*) FROM graphos_meta.edge_endpoints WHERE tenant = $1)"
            " + (SELECT count(*) FROM graphos_meta.tenant_settings WHERE tenant = $1);",
            TENANT,
        )
    assert left == 0

"""DB tests: an agent reads a tenant through ``/mcp`` with a service account's token.

DB-gated.  The identity tables are instance-wide, so each test starts with no
user, grant, session or signing key, as a first start would.  The agent is
the MCP SDK's own client over an in-process ASGI transport; its token comes
from ``POST /v1/auth/login``, as for the REST API (ADR-0008).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import asyncpg
import httpx
import httpx2
import pytest
import pytest_asyncio
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from graphos import console as console_mod
from graphos import writes
from graphos.app import create_app, setup_identity
from graphos.bootstrap import TenantSpec, forget, register_tenant
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_mcp"
OTHER = "graphos_test_mcp_other"
FIRST, SECOND = "first-password-1", "second-password-2"


async def _wipe(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "DELETE FROM graphos_meta.users; DELETE FROM graphos_meta.grants; "
        "DELETE FROM graphos_meta.sessions; DELETE FROM graphos_meta.signing_keys;",
    )
    async with pool.acquire() as conn:
        for name in (TENANT, OTHER):
            if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", name):
                await conn.execute(f"SELECT drop_graph('{name}', true);")
            await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", name)
            forget(name)


@pytest_asyncio.fixture
async def app(db_pool: asyncpg.Pool, tmp_path: Path) -> AsyncIterator[Any]:
    await _wipe(db_pool)
    for name in (TENANT, OTHER):
        await register_tenant(db_pool, TenantSpec(
            name=name, vertex_labels=("Doc",), edge_labels=("LINKS_TO",),
        ))
    await writes.upsert_nodes(db_pool, tenant=TENANT, label="Doc", rows=[
        {"id": "a", "props": {"name": "alpha", "status": "ACT"}},
        {"id": "b", "props": {"name": "beta", "status": "ACT"}},
        {"id": "c", "props": {"name": "gamma", "status": "OLD"}},
    ])
    await writes.upsert_edges(
        db_pool, tenant=TENANT, src_label="Doc", dst_label="Doc", edge_type="LINKS_TO",
        rows=[{"src_id": "a", "dst_id": "b", "props": {}}],
    )
    password_file = tmp_path / "admin_password"
    password_file.write_text(FIRST + "\n")
    dsn = urlparse(os.environ["GRAPHOS_TEST_DSN"])
    settings = Settings(
        db_host=dsn.hostname, db_port=dsn.port or 5432, db_name=dsn.path.lstrip("/"),
        db_user=unquote(dsn.username or ""), db_password=unquote(dsn.password or ""),
        admin_password_file=password_file,
    )
    application = create_app(settings)
    application.state.pool = db_pool
    await setup_identity(application, db_pool, settings)
    yield application
    await _wipe(db_pool)


def _rest(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")


async def _service_secret(rest: httpx.AsyncClient, saved: dict[str, Any] | None = None) -> str:
    """Admin signs in, creates service account "agent" with read on TENANT (and saves
    ``saved`` as query "active_docs"): its secret."""
    first = (await rest.post("/v1/auth/login", json={"user": "graphos", "password": FIRST})).json()
    admin = (await rest.post(
        "/v1/me/password", headers={"Authorization": f"Bearer {first['access_token']}"},
        json={"current": FIRST, "new": SECOND},
    )).json()["access_token"]
    auth = {"Authorization": f"Bearer {admin}"}
    created = await rest.post("/v1/users", headers=auth, json={"name": "agent", "kind": "service"})
    assert created.status_code == HTTPStatus.CREATED, created.text
    granted = await rest.post("/v1/grants", headers=auth, json={
        "subject": "user:agent", "tenant": TENANT, "level": "read",
    })
    assert granted.status_code in (HTTPStatus.OK, HTTPStatus.CREATED), granted.text
    if saved is not None:
        put = await rest.put(f"/v1/{TENANT}/cypher/saved/active_docs", headers=auth, json=saved)
        assert put.status_code in (HTTPStatus.OK, HTTPStatus.CREATED), put.text
    return str(created.json()["password"])


async def _token(rest: httpx.AsyncClient, secret: str) -> str:
    """The service account's token, from the same sign-in as for REST."""
    resp = await rest.post("/v1/auth/login", json={"user": "agent", "password": secret})
    assert resp.status_code == HTTPStatus.OK, resp.text
    return str(resp.json()["access_token"])


def _mcp(app: Any, token: str) -> Client:
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), headers={"Authorization": f"Bearer {token}"},
    )
    return Client(streamable_http_client("http://localhost/mcp", http_client=http))


@requires_db
@pytest.mark.asyncio
async def test_an_agent_reads_a_granted_tenant_and_every_call_is_audited(
    app: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    async with _rest(app) as rest:
        token = await _token(rest, await _service_secret(rest))
    caplog.set_level(logging.INFO, logger="graphos.audit")
    async with app.state.mcp.manager.run(), _mcp(app, token) as client:
        tenants = await client.call_tool("list_tenants", {})
        assert tenants.structured_content["tenants"] == [TENANT]
        schema = await client.call_tool("get_schema", {"tenant": TENANT})
        assert not schema.is_error, schema.content
        (doc,) = schema.structured_content["labels"]
        status = next(p for p in doc["properties"] if p["name"] == "status")
        assert {v["value"]: v["count"] for v in status["values"]} == {"ACT": 2, "OLD": 1}
        assert "status" in schema.structured_content["text"]
        found = await client.call_tool("search_nodes", {"tenant": TENANT, "q": "alp"})
        assert [n["properties"]["id"] for n in found.structured_content["nodes"]] == ["a"]
        checked = await client.call_tool("check_edges", {"tenant": TENANT, "edges": [
            {"type": "LINKS_TO", "src_id": "a", "dst_id": "b"},
            {"type": "LINKS_TO", "src_id": "b", "dst_id": "a"},
        ]})
        assert [r["status"] for r in checked.structured_content["results"]] == [
            "present", "absent",
        ]
        refused = await client.call_tool("get_schema", {"tenant": OTHER})
        assert refused.is_error                        # no grant on the other tenant
        summary = await client.read_resource(f"graphos://{TENANT}/schema/summary")
        assert "Doc" in summary.contents[0].text
        graph_map = await client.call_tool("get_graph_map", {"tenant": TENANT})
        assert graph_map.structured_content["labels"] == [{"label": "Doc", "count": 3}]
        stats = await client.call_tool("get_graph_stats", {"tenant": TENANT})
        assert stats.structured_content["n_vertices"] == 3  # noqa: PLR2004
        changes = await client.call_tool("recent_changes", {"tenant": TENANT, "limit": 5})
        assert not changes.is_error, changes.content
    tools = [r.mcp_tool for r in caplog.records if hasattr(r, "mcp_tool")]
    assert {"list_tenants", "get_schema", "search_nodes", "check_edges"} <= set(tools)
    assert all(getattr(r, "consumer", None) == "agent"
               for r in caplog.records if hasattr(r, "mcp_tool"))


@requires_db
@pytest.mark.asyncio
async def test_the_token_that_opens_rest_opens_mcp_and_nothing_else_does(app: Any) -> None:
    async with _rest(app) as rest:
        token = await _token(rest, await _service_secret(rest))
        assert (await rest.get(
            "/v1/me", headers={"Authorization": f"Bearer {token}"},
        )).status_code == HTTPStatus.OK
        gone = await rest.post("/v1/auth/token", data={"grant_type": "client_credentials"})
        assert gone.status_code in (HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED)
        for bad in ("not-a-token", token[:-4] + "AAAA"):
            refused = await rest.post("/mcp", json={}, headers={"Authorization": f"Bearer {bad}"})
            assert refused.status_code == HTTPStatus.UNAUTHORIZED
    async with app.state.mcp.manager.run(), _mcp(app, token) as client:
        tenants = await client.call_tool("list_tenants", {})
        assert tenants.structured_content["tenants"] == [TENANT]


@requires_db
@pytest.mark.asyncio
async def test_run_cypher_is_offered_with_the_console_and_keeps_its_guard(
    app: Any, db_pool: asyncpg.Pool,
) -> None:
    settings = app.state.settings.model_copy(update={"cypher": True})
    app.state.settings = settings
    app.state.mcp.settings = settings
    app.state.console = await console_mod.start(settings, db_pool)
    try:
        saved = {"description": "Documents in a status", "params": {"s": {"type": "string"}},
                 "query": "MATCH (d:Doc) WHERE d.status = $s RETURN d.name ORDER BY d.name"}
        async with _rest(app) as rest:
            secret = await _service_secret(rest, saved)
            token = await _token(rest, secret)
        async with app.state.mcp.manager.run(), _mcp(app, token) as client:
            assert "run_cypher" in [t.name for t in (await client.list_tools()).tools]
            listed = await client.call_tool("list_saved_queries", {"tenant": TENANT})
            assert [q["name"] for q in listed.structured_content["result"]] == ["active_docs"]
            one = await client.call_tool(
                "get_saved_query", {"tenant": TENANT, "name": "active_docs"},
            )
            assert one.structured_content["query"] == saved["query"]
            ran = await client.call_tool("run_saved_query", {
                "tenant": TENANT, "name": "active_docs", "params": {"s": "ACT"},
            })
            assert ran.structured_content["rows"] == [["alpha"], ["beta"]]
            missing = await client.call_tool("get_saved_query", {"tenant": TENANT, "name": "nope"})
            assert missing.is_error
            resource = await client.read_resource(f"graphos://{TENANT}/saved-queries")
            assert "active_docs" in resource.contents[0].text
            counted = await client.call_tool("run_cypher", {
                "tenant": TENANT, "query": "MATCH (d:Doc) WHERE d.status = $s RETURN count(d)",
                "params": {"s": "ACT"},
            })
            assert not counted.is_error, counted.content
            assert counted.structured_content["rows"] == [[2]]
            for query in ("CREATE (n:Doc {id: 'x'}) RETURN n",
                          "MATCH (a:Doc)-[*1..3]->(b) RETURN b"):
                refused = await client.call_tool("run_cypher", {"tenant": TENANT, "query": query})
                assert refused.is_error, query
    finally:
        await app.state.console.pool.close()

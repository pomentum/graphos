"""DB tests: an agent reads a tenant through ``/mcp`` with a service account's token.

DB-gated.  The identity tables are instance-wide, so each test starts with no
user, grant, session or signing key, as a first start would.  The agent is
the MCP SDK's own client over an in-process ASGI transport; its token comes
from ``POST /v1/auth/token`` (client credentials, ``resource`` = the MCP
endpoint), so the audience rules are those of a real deployment.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import httpx2
import pytest
import pytest_asyncio
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from graphos import writes
from graphos.app import create_app, setup_identity
from graphos.bootstrap import TenantSpec, forget, register_tenant
from graphos.config import Settings

from .conftest import requires_db

RESOURCE = "https://graph.example.com/mcp"
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
    settings = Settings(
        db_password="placeholder", admin_password_file=password_file,
        mcp=True, mcp_resource=RESOURCE,
    )
    application = create_app(settings)
    application.state.pool = db_pool
    await setup_identity(application, db_pool, settings)
    yield application
    await _wipe(db_pool)


def _rest(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")


async def _service_secret(rest: httpx.AsyncClient) -> str:
    """Admin signs in, creates service account "agent" with read on TENANT: its secret."""
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
    return str(created.json()["password"])


async def _token(rest: httpx.AsyncClient, secret: str, resource: str | None) -> httpx.Response:
    form = {"grant_type": "client_credentials", "client_id": "agent", "client_secret": secret}
    if resource is not None:
        form["resource"] = resource
    return await rest.post("/v1/auth/token", data=form)


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
        secret = await _service_secret(rest)
        resp = await _token(rest, secret, RESOURCE)
        assert resp.status_code == HTTPStatus.OK, resp.text
        token = resp.json()["access_token"]
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
    tools = [r.mcp_tool for r in caplog.records if hasattr(r, "mcp_tool")]
    assert {"list_tenants", "get_schema", "search_nodes", "check_edges"} <= set(tools)
    assert all(getattr(r, "consumer", None) == "agent"
               for r in caplog.records if hasattr(r, "mcp_tool"))


@requires_db
@pytest.mark.asyncio
async def test_a_token_opens_only_the_resource_it_was_issued_for(app: Any) -> None:
    async with _rest(app) as rest:
        secret = await _service_secret(rest)
        rest_token = (await _token(rest, secret, None)).json()["access_token"]
        mcp_token = (await _token(rest, secret, RESOURCE)).json()["access_token"]
        on_rest = await rest.get("/v1/me", headers={"Authorization": f"Bearer {mcp_token}"})
        assert on_rest.status_code == HTTPStatus.UNAUTHORIZED
        on_mcp = await rest.post(
            "/mcp", json={}, headers={"Authorization": f"Bearer {rest_token}"},
        )
        assert on_mcp.status_code == HTTPStatus.UNAUTHORIZED
        wrong = await _token(rest, "not-the-secret", RESOURCE)
        assert (wrong.status_code, wrong.json()["error"]) == (401, "invalid_client")
        elsewhere = await _token(rest, secret, "https://elsewhere.example/mcp")
        assert (elsewhere.status_code, elsewhere.json()["error"]) == (400, "invalid_target")
        person = await rest.post("/v1/auth/token", data={
            "grant_type": "client_credentials", "client_id": "graphos", "client_secret": SECOND,
        })
        assert person.status_code == HTTPStatus.UNAUTHORIZED     # not a service account

"""The MCP endpoint without a database: the tool list, sign-in and Origin (ADR-0002).

Driven with the MCP SDK's own client over an in-process ASGI transport, as an
agent would call it.  Calls that read a tenant are in ``test_mcp_db.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from graphos import mcp_server
from graphos.app import create_app
from graphos.auth import StaticAuthenticator
from graphos.config import Settings
from graphos.identity import Level, Principal

RESOURCE = "https://graph.example.com/mcp"
READER = Principal(consumer="reader", subject="user:reader", levels=(("acme", Level.READ),))


def _app(**settings: object) -> object:
    app = create_app(Settings(db_password="x", mcp=True, mcp_resource=RESOURCE, **settings))
    app.state.authenticator = StaticAuthenticator({"tok": READER})
    return app


@asynccontextmanager
async def _client(app: object, token: str | None = "tok") -> AsyncIterator[Client]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with app.state.mcp.manager.run(), httpx2.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx2.ASGITransport(app=app), headers=headers,
    ) as http, Client(streamable_http_client("http://localhost/mcp", http_client=http)) as c:
        yield c


def test_every_tool_is_a_read_with_a_static_description() -> None:
    tools = mcp_server.tool_list()
    names = [t.name for t in tools]
    assert len(names) == len(set(names))
    assert {"list_tenants", "get_schema", "check_edges", "get_property_values"} <= set(names)
    for spec, tool in zip(mcp_server.TOOLS, tools, strict=True):
        assert spec.access == "read"
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.description and tool.input_schema["type"] == "object"
        assert ("tenant" in tool.input_schema["required"]) is spec.needs_tenant


@pytest.mark.asyncio
async def test_an_agent_lists_the_tools_and_the_resources() -> None:
    async with _client(_app()) as client:
        tools = await client.list_tools()
        templates = await client.list_resource_templates()
    assert [t.name for t in tools.tools] == [t.name for t in mcp_server.TOOLS]
    assert {t.uri_template for t in templates.resource_templates} == {
        "graphos://{tenant}/schema/summary", "graphos://{tenant}/properties",
    }


@pytest.mark.asyncio
async def test_without_a_token_for_the_endpoint_it_answers_401_with_its_metadata() -> None:
    app = _app()
    async with app.state.mcp.manager.run(), httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.ASGITransport(app=app), base_url="http://localhost",
    ) as http:
        for headers in ({}, {"Authorization": "Bearer nope"}):
            r = await http.post("/mcp", json={}, headers=headers)
            assert r.status_code == 401  # noqa: PLR2004
            assert r.headers["www-authenticate"] == (
                'Bearer resource_metadata="https://graph.example.com/.well-known/'
                'oauth-protected-resource/mcp"'
            )
        meta = await http.get("/.well-known/oauth-protected-resource/mcp")
        assert meta.json()["resource"] == RESOURCE
        bad_origin = await http.post(
            "/mcp", json={}, headers={"Authorization": "Bearer tok", "Origin": "https://evil.example"},
        )
        assert bad_origin.status_code == 403  # noqa: PLR2004


def test_the_endpoint_needs_its_public_url() -> None:
    with pytest.raises(RuntimeError, match="GRAPHOS_MCP_RESOURCE"):
        create_app(Settings(db_password="x", mcp=True))

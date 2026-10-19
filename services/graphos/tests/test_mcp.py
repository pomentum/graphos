"""The MCP endpoint without a database: the tool list, sign-in and Origin (ADR-0002, ADR-0008).

Driven with the MCP SDK's own client over an in-process ASGI transport, as an
agent would call it.  Calls that read a tenant are in ``test_mcp_db.py``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

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

READER = Principal(consumer="reader", subject="user:reader", levels=(("acme", Level.READ),))
METADATA = "http://localhost/.well-known/oauth-protected-resource/mcp"


def _app(**settings: object) -> Any:
    app = create_app(Settings(db_password="x", **settings))
    app.state.authenticator = StaticAuthenticator({"tok": READER})
    return app


@asynccontextmanager
async def _client(app: Any, token: str | None = "tok") -> AsyncIterator[Client]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with app.state.mcp.manager.run(), httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), headers=headers,
    ) as http, Client(streamable_http_client("http://localhost/mcp", http_client=http)) as c:
        yield c


def test_every_tool_is_a_read_with_a_static_description() -> None:
    tools = mcp_server.tool_list(Settings(db_password="x", cypher=True))
    names = [t.name for t in tools]
    assert len(names) == len(set(names))
    assert {"list_tenants", "get_schema", "check_edges", "get_property_values",
            "run_cypher"} <= set(names)
    off = Settings(db_password="x", cypher=False)
    assert "run_cypher" not in {t.name for t in mcp_server.tool_list(off)}
    for spec, tool in zip(mcp_server.TOOLS, tools, strict=True):
        assert spec.access == "read"
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.description and tool.input_schema["type"] == "object"
        assert ("tenant" in tool.input_schema["required"]) is spec.needs_tenant


def _sample(schema: dict[str, Any]) -> Any:
    kind = str(schema.get("type"))
    return {"integer": 1, "number": 1, "boolean": True, "array": ["x"], "object": {}}.get(kind, "x")


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        return _resolve(spec, spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]])
    return schema


def test_every_tool_argument_is_a_parameter_of_its_route() -> None:
    """The tools' input schemas are written for models, apart from the routes'
    own; a query parameter a route no longer has is ignored without an error,
    so every argument a tool sends must still be one its route declares."""
    spec = create_app(Settings(db_password="x")).openapi()
    # Literal paths first, as the router matches them (`nodes/aggregate` before `nodes/{id}`).
    templates = sorted(
        ((path.count("{"), re.compile(
            "^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(path)) + "$"), ops)
         for path, ops in spec["paths"].items()),
        key=lambda t: t[0],
    )
    for tool in mcp_server.TOOLS:
        args = {k: _sample(v) for k, v in tool.input_schema["properties"].items()}
        method, path, query, body = tool.route(args)
        op = next(
            (o[method.lower()] for _, rx, o in templates if rx.match(path) and method.lower() in o),
            None,
        )
        assert op is not None, (tool.name, method, path)
        declared = {p["name"] for p in op.get("parameters", []) if p["in"] == "query"}
        assert set(query) <= declared, (tool.name, set(query) - declared)
        if body:
            schema = _resolve(spec, op["requestBody"]["content"]["application/json"]["schema"])
            fields = set(schema.get("properties", {}))
            assert set(body) <= fields, (tool.name, set(body) - fields)


@pytest.mark.asyncio
async def test_an_agent_lists_the_tools_and_the_resources() -> None:
    async with _client(_app()) as client:
        tools = await client.list_tools()
        templates = await client.list_resource_templates()
    assert [t.name for t in tools.tools] == [t.name for t in mcp_server.TOOLS]
    assert "run_cypher" in [t.name for t in tools.tools]          # the console is on by default
    async with _client(_app(cypher=False)) as client:
        off = await client.list_tools()
    assert "run_cypher" not in [t.name for t in off.tools]
    assert {t.uri_template for t in templates.resource_templates} == {
        "graphos://{tenant}/schema/summary", "graphos://{tenant}/properties",
        "graphos://{tenant}/saved-queries",
    }


@pytest.mark.asyncio
async def test_without_a_valid_token_it_answers_401_with_its_metadata() -> None:
    app = _app()
    async with app.state.mcp.manager.run(), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost",
    ) as http:
        for headers in ({}, {"Authorization": "Bearer nope"}):
            r = await http.post("/mcp", json={}, headers=headers)
            assert r.status_code == 401  # noqa: PLR2004
            assert r.headers["www-authenticate"] == f'Bearer resource_metadata="{METADATA}"'
        meta = await http.get("/.well-known/oauth-protected-resource/mcp")
        assert meta.json()["resource"] == "http://localhost/mcp"


@pytest.mark.asyncio
async def test_origin_follows_the_host_allowlist_and_a_star_turns_both_off() -> None:
    for allowed, origin, ok in (
        ("", "http://localhost", True),                  # no list: the request's own host
        ("", "https://evil.example", False),
        ("graph.example.com", "https://graph.example.com", True),
        ("graph.example.com", "https://evil.example", False),
        ("*.example.com", "https://a.example.com", True),
        ("*", "http://localhost", True),                 # off, as the Host check
        ("*", "https://evil.example", False),
    ):
        app = _app(allowed_hosts=allowed)
        async with app.state.mcp.manager.run(), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
        ) as http:
            r = await http.post(
                "/mcp", json={}, headers={"Authorization": "Bearer nope", "Origin": origin},
            )
        assert (r.status_code != 403) is ok, (allowed, origin, r.status_code)  # noqa: PLR2004


def test_mcp_is_on_by_default_and_can_be_turned_off() -> None:
    assert Settings(db_password="x").mcp is True                  # ADR-0008
    assert getattr(create_app(Settings(db_password="x", mcp=False)).state, "mcp", None) is None

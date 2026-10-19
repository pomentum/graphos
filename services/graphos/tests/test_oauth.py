"""graphos as MCP clients' authorization server, without a database (ADR-0008).

Which redirect URIs may register, which server the metadata names, and that
the authorization server is mounted only where graphos signs clients in.
The flow itself is in ``test_oauth_db.py``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from graphos.app import create_app
from graphos.config import Settings
from graphos.oauth import redirect_allowed


@pytest.mark.parametrize(
    ("uri", "listed", "allowed"),
    [
        ("http://127.0.0.1:33418/callback", [], True),
        ("http://localhost:6274/oauth/callback", [], True),
        ("http://[::1]:8080/cb", [], True),
        ("https://127.0.0.1/cb", [], False),                  # loopback is plain http
        ("http://127.0.0.1.evil.example/cb", [], False),
        ("https://app.example.com/cb", [], False),
        ("https://app.example.com/cb", ["https://app.example.com/cb"], True),
        ("https://app.example.com/cb2", ["https://app.example.com/cb"], False),
        ("cursor://callback", [], False),
    ],
)
def test_only_loopback_and_listed_redirects_may_register(
    uri: str, listed: list[str], allowed: bool,  # noqa: FBT001
) -> None:
    assert redirect_allowed(uri, listed) is allowed


async def _get(app: Any, path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://graph.example.com",
    ) as http:
        return await http.get(path)


@pytest.mark.asyncio
async def test_without_single_sign_on_graphos_is_the_authorization_server() -> None:
    app = create_app(Settings(db_password="x"))
    resource = (await _get(app, "/.well-known/oauth-protected-resource/mcp")).json()
    assert resource["authorization_servers"] == ["https://graph.example.com"]
    meta = (await _get(app, "/.well-known/oauth-authorization-server")).json()
    assert meta["issuer"] == "https://graph.example.com"
    assert meta["authorization_endpoint"] == "https://graph.example.com/oauth/authorize"
    assert meta["token_endpoint"] == "https://graph.example.com/oauth/token"
    assert meta["registration_endpoint"] == "https://graph.example.com/oauth/register"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]


@pytest.mark.asyncio
async def test_with_single_sign_on_the_provider_signs_clients_in() -> None:
    app = create_app(Settings(
        db_password="x", oidc_issuer="https://idp.example/realms/r", oidc_client_id="graphos",
    ))
    resource = (await _get(app, "/.well-known/oauth-protected-resource/mcp")).json()
    assert resource["authorization_servers"] == ["https://idp.example/realms/r"]
    assert (await _get(app, "/.well-known/oauth-authorization-server")).status_code == 404  # noqa: PLR2004
    assert (await _get(app, "/oauth/authorize")).status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_with_mcp_off_nothing_signs_clients_in() -> None:
    app = create_app(Settings(db_password="x", mcp=False))
    assert (await _get(app, "/.well-known/oauth-authorization-server")).status_code == 404  # noqa: PLR2004
    assert (await _get(app, "/oauth/login")).status_code == 404  # noqa: PLR2004

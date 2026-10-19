"""DB tests: an MCP client signs a person in through graphos (ADR-0008, ``graphos.oauth``).

DB-gated.  The identity tables are instance-wide, so each test starts with no
user, grant, session, signing key or OAuth client, as a first start would.
The client registers, sends the "browser" through ``/oauth/authorize`` and
the sign-in page, exchanges the code, calls ``/mcp``, renews and revokes,
over an in-process ASGI transport.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from collections.abc import AsyncIterator
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import asyncpg
import httpx
import httpx2
import pytest
import pytest_asyncio
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from graphos.app import create_app, setup_identity
from graphos.config import Settings

from .conftest import requires_db

FIRST, SECOND = "first-password-1", "second-password-2"
CALLBACK = "http://127.0.0.1:33418/callback"


async def _wipe(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "DELETE FROM graphos_meta.users; DELETE FROM graphos_meta.grants; "
        "DELETE FROM graphos_meta.sessions; DELETE FROM graphos_meta.signing_keys; "
        "DELETE FROM graphos_meta.oauth_clients; DELETE FROM graphos_meta.oauth_codes;",
    )


@pytest_asyncio.fixture
async def app(db_pool: asyncpg.Pool, tmp_path: Path) -> AsyncIterator[Any]:
    await _wipe(db_pool)
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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost",
    ) as http:
        first = (await http.post(
            "/v1/auth/login", json={"user": "graphos", "password": FIRST},
        )).json()
        await http.post(
            "/v1/me/password", headers={"Authorization": f"Bearer {first['access_token']}"},
            json={"current": FIRST, "new": SECOND},
        )
    yield application
    await _wipe(db_pool)


def _http(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost",
        follow_redirects=False,
    )


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


async def _register(http: httpx.AsyncClient, redirect: str = CALLBACK) -> httpx.Response:
    return await http.post("/oauth/register", json={
        "redirect_uris": [redirect], "client_name": "Test client",
        "grant_types": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_method": "client_secret_post",
    })


async def _sign_in_page(
    http: httpx.AsyncClient, client_id: str, challenge: str,
) -> dict[str, str]:
    """``/oauth/authorize`` → the sign-in page; its hidden fields."""
    authorize = await http.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s1",
    })
    assert authorize.status_code == HTTPStatus.FOUND, authorize.text
    login = urlparse(authorize.headers["location"])
    assert login.path == "/oauth/login"
    page = await http.get(authorize.headers["location"])
    assert page.status_code == HTTPStatus.OK
    assert "Test client" in page.text and "127.0.0.1:33418" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    return {k: v[0] for k, v in parse_qs(login.query).items()}


@requires_db
@pytest.mark.asyncio
async def test_an_mcp_client_signs_a_person_in_and_renews(app: Any) -> None:
    async with _http(app) as http:
        registered = await _register(http)
        assert registered.status_code == HTTPStatus.CREATED, registered.text
        client = registered.json()
        assert client["token_endpoint_auth_method"] == "none"      # public, whatever it asked
        assert "client_secret" not in client
        verifier, challenge = _pkce()
        fields = await _sign_in_page(http, client["client_id"], challenge)

        wrong = await http.post("/oauth/login", data={
            **fields, "user": "graphos", "password": "nope", "action": "allow",
        })
        assert wrong.status_code == HTTPStatus.UNAUTHORIZED
        signed = await http.post("/oauth/login", data={
            **fields, "user": "graphos", "password": SECOND, "action": "allow",
        })
        assert signed.status_code == HTTPStatus.FOUND, signed.text
        back = urlparse(signed.headers["location"])
        assert f"{back.scheme}://{back.netloc}{back.path}" == CALLBACK
        query = parse_qs(back.query)
        assert query["state"] == ["s1"]

        exchange = {
            "grant_type": "authorization_code", "code": query["code"][0],
            "redirect_uri": CALLBACK, "client_id": client["client_id"], "code_verifier": verifier,
        }
        tokens = await http.post("/oauth/token", data=exchange)
        assert tokens.status_code == HTTPStatus.OK, tokens.text
        issued = tokens.json()
        assert issued["refresh_token"].startswith(client["client_id"] + ".")
        again = await http.post("/oauth/token", data=exchange)
        assert again.status_code == HTTPStatus.BAD_REQUEST
        assert again.json()["error"] == "invalid_grant"           # a code is used once

        me = await http.get("/v1/me", headers={"Authorization": f"Bearer {issued['access_token']}"})
        assert me.json()["consumer"] == "graphos"

        renewed = await http.post("/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": issued["refresh_token"],
            "client_id": client["client_id"],
        })
        assert renewed.status_code == HTTPStatus.OK, renewed.text
        fresh = renewed.json()
        assert fresh["refresh_token"] != issued["refresh_token"]

        other = (await _register(http)).json()
        stolen = await http.post("/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": fresh["refresh_token"],
            "client_id": other["client_id"],
        })
        assert stolen.status_code == HTTPStatus.BAD_REQUEST        # bound to its client

        revoked = await http.post("/oauth/revoke", data={
            "token": fresh["refresh_token"], "client_id": client["client_id"],
        })
        assert revoked.status_code == HTTPStatus.OK
        after = await http.post("/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": fresh["refresh_token"],
            "client_id": client["client_id"],
        })
        assert after.status_code == HTTPStatus.BAD_REQUEST

    mcp_http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        headers={"Authorization": f"Bearer {fresh['access_token']}"},
    )
    async with app.state.mcp.manager.run(), Client(
        streamable_http_client("http://localhost/mcp", http_client=mcp_http),
    ) as mcp:
        tenants = await mcp.call_tool("list_tenants", {})
        assert not tenants.is_error, tenants.content


@requires_db
@pytest.mark.asyncio
async def test_a_redirect_that_is_not_loopback_or_listed_is_refused(app: Any) -> None:
    async with _http(app) as http:
        refused = await _register(http, "https://evil.example/callback")
        assert refused.status_code == HTTPStatus.BAD_REQUEST
        assert refused.json()["error"] == "invalid_redirect_uri"
        client = (await _register(http)).json()
        _, challenge = _pkce()
        fields = await _sign_in_page(http, client["client_id"], challenge)
        swapped = await http.post("/oauth/login", data={
            **fields, "redirect_uri": "https://evil.example/callback",
            "user": "graphos", "password": SECOND, "action": "allow",
        })
        assert swapped.status_code == HTTPStatus.BAD_REQUEST     # checked again on submit
        denied = await http.post("/oauth/login", data={**fields, "action": "deny"})
        assert denied.status_code == HTTPStatus.FOUND
        assert parse_qs(urlparse(denied.headers["location"]).query)["error"] == ["access_denied"]


@requires_db
@pytest.mark.asyncio
async def test_a_service_account_is_sent_to_the_login_api(app: Any) -> None:
    async with _http(app) as http:
        admin = (await http.post(
            "/v1/auth/login", json={"user": "graphos", "password": SECOND},
        )).json()["access_token"]
        created = await http.post(
            "/v1/users", headers={"Authorization": f"Bearer {admin}"},
            json={"name": "agent", "kind": "service"},
        )
        secret = created.json()["password"]
        client = (await _register(http)).json()
        _, challenge = _pkce()
        fields = await _sign_in_page(http, client["client_id"], challenge)
        page = await http.post("/oauth/login", data={
            **fields, "user": "agent", "password": secret, "action": "allow",
        })
        assert page.status_code == HTTPStatus.FORBIDDEN
        assert "/v1/auth/login" in page.text

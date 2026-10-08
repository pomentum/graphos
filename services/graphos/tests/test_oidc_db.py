"""Single sign-on end to end, against a database and the fake identity provider."""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos.app import create_app, setup_identity
from graphos.config import Settings

from .conftest import requires_db
from .fake_idp import AUDIENCE, CLIENT_ID, ISSUER, FakeIdp
from .test_identity_db import FIRST, SECOND, _wipe

CALLBACK = "https://ui.test/auth/callback"


@pytest_asyncio.fixture
async def sso(
    db_pool: asyncpg.Pool, tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeIdp]]:
    await _wipe(db_pool)
    await db_pool.execute("DELETE FROM graphos_meta.oidc_pending;")
    password_file = tmp_path / "admin_password"
    password_file.write_text(FIRST)
    settings = Settings(
        db_password="placeholder", admin_password_file=password_file,
        oidc_issuer=ISSUER, oidc_client_id=CLIENT_ID, oidc_audience=AUDIENCE,
        oidc_display_name="Acme SSO", oidc_redirect_uris=[CALLBACK],
    )
    idp = FakeIdp()
    app = create_app(settings)
    app.state.pool = db_pool
    await setup_identity(app, db_pool, settings, oidc_transport=idp.transport())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        yield client, idp
    await _wipe(db_pool)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin(api: httpx.AsyncClient) -> str:
    first = (await api.post("/v1/auth/login", json={"user": "graphos", "password": FIRST})).json()
    changed = await api.post("/v1/me/password", headers=_auth(first["access_token"]),
                             json={"current": FIRST, "new": SECOND})
    return changed.json()["access_token"]


async def _grant(api: httpx.AsyncClient, admin: str, subject: str, tenant: str, level: str) -> None:
    resp = await api.post("/v1/grants", headers=_auth(admin),
                          json={"subject": subject, "tenant": tenant, "level": level})
    assert resp.status_code == HTTPStatus.CREATED, resp.text


async def _start(api: httpx.AsyncClient, **body: Any) -> str:
    resp = await api.post("/v1/auth/oidc/start",
                          json={"redirect_uri": CALLBACK, "return_to": "/explore", **body})
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()["authorization_url"]


async def _sso_sign_in(api: httpx.AsyncClient, idp: FakeIdp, **claims: Any) -> dict[str, Any]:
    code, state = idp.approve(await _start(api), **claims)
    resp = await api.post("/v1/auth/oidc/callback", json={"state": state, "code": code})
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()


@requires_db
@pytest.mark.asyncio
async def test_config_offers_single_sign_on(sso) -> None:  # noqa: ANN001
    api, _ = sso
    body = (await api.get("/v1/auth/config")).json()
    assert body == {"password": True, "oidc": {
        "issuer": ISSUER, "client_id": CLIENT_ID, "scopes": "openid profile email",
        "display_name": "Acme SSO",
    }}


@requires_db
@pytest.mark.asyncio
async def test_a_person_signs_in_and_gets_the_access_of_their_groups(sso) -> None:  # noqa: ANN001
    api, idp = sso
    admin = await _admin(api)
    await _grant(api, admin, "oidc-group:analysts", "sol_*", "create_tenants")
    await _grant(api, admin, "oidc-role:graph-admin", "*", "admin")
    signed = await _sso_sign_in(api, idp, preferred_username="ada", groups=["analysts"])
    assert signed["return_to"] == "/explore" and signed["refresh_token"]
    # graphos exchanged the code itself, with the PKCE verifier and as a public client.
    (exchange,) = idp.token_requests
    assert exchange["client_id"] == CLIENT_ID and exchange["code_verifier"]
    assert "client_secret" not in exchange
    me = (await api.get("/v1/me", headers=_auth(signed["access_token"]))).json()
    assert (me["consumer"], me["via"], me["admin"], me["create_tenants"]) == (
        "ada", "oidc", False, ["sol_*"],
    )
    # Roles carried only in the provider's access token count too.
    boss = await _sso_sign_in(api, idp, sub="u-7", preferred_username="bo", roles=["graph-admin"])
    assert (await api.get("/v1/me", headers=_auth(boss["access_token"]))).json()["admin"] is True
    # No graphos password to change.
    resp = await api.post("/v1/me/password", headers=_auth(signed["access_token"]),
                          json={"current": "x", "new": "something-long-1"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


@requires_db
@pytest.mark.asyncio
async def test_a_silent_sign_in_that_needs_a_prompt_is_refused(sso) -> None:  # noqa: ANN001
    api, _ = sso
    url = await _start(api, silent=True)
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert query["prompt"] == "none"
    resp = await api.post("/v1/auth/oidc/callback",
                          json={"state": query["state"], "error": "login_required"})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    assert "login_required" in resp.json()["detail"]


@requires_db
@pytest.mark.asyncio
async def test_a_sign_in_is_used_once_and_checks_the_nonce(sso) -> None:  # noqa: ANN001
    api, idp = sso
    code, state = idp.approve(await _start(api))
    assert (await api.post("/v1/auth/oidc/callback",
                           json={"state": state, "code": code})).status_code == HTTPStatus.OK
    replay = await api.post("/v1/auth/oidc/callback", json={"state": state, "code": code})
    assert replay.status_code == HTTPStatus.UNAUTHORIZED
    # An id token for another sign-in (wrong nonce) is refused.
    code, state = idp.approve(await _start(api), nonce="someone-elses")
    wrong = await api.post("/v1/auth/oidc/callback", json={"state": state, "code": code})
    assert wrong.status_code == HTTPStatus.UNAUTHORIZED
    unknown = await api.post("/v1/auth/oidc/callback", json={"state": "made-up", "code": "x"})
    assert unknown.status_code == HTTPStatus.UNAUTHORIZED


@requires_db
@pytest.mark.asyncio
async def test_redirects_are_limited(sso) -> None:  # noqa: ANN001
    api, idp = sso
    resp = await api.post("/v1/auth/oidc/start", json={"redirect_uri": "https://evil.test/cb"})
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    # Only a page of the UI to come back to.
    url = (await api.post("/v1/auth/oidc/start", json={
        "redirect_uri": CALLBACK, "return_to": "//evil.test/x",
    })).json()["authorization_url"]
    code, state = idp.approve(url)
    back = await api.post("/v1/auth/oidc/callback", json={"state": state, "code": code})
    assert back.json()["return_to"] == "/"


@requires_db
@pytest.mark.asyncio
async def test_an_sso_session_renews_and_signs_out_at_the_provider(sso) -> None:  # noqa: ANN001
    api, idp = sso
    signed = await _sso_sign_in(api, idp, preferred_username="ada")
    renewed = await api.post("/v1/auth/refresh", json={"refresh_token": signed["refresh_token"]})
    assert renewed.status_code == HTTPStatus.OK
    me = (await api.get("/v1/me", headers=_auth(renewed.json()["access_token"]))).json()
    assert (me["consumer"], me["via"]) == ("ada", "oidc")
    out = await api.post("/v1/auth/logout", json={
        "refresh_token": renewed.json()["refresh_token"],
        "post_logout_redirect_uri": "https://ui.test/",
    })
    end = out.json()["end_session_url"]
    assert end.startswith(f"{ISSUER}/protocol/openid-connect/logout?")
    # The ID token goes back as the hint, so the provider signs out without asking.
    hint = parse_qs(urlsplit(end).query)["id_token_hint"][0]
    assert hint.count(".") == 2
    last = renewed.json()["refresh_token"]
    gone = await api.post("/v1/auth/refresh", json={"refresh_token": last})
    assert gone.status_code == HTTPStatus.UNAUTHORIZED
    # A graphos user's session has no provider to sign out of.
    first = (await api.post("/v1/auth/login", json={"user": "graphos", "password": FIRST})).json()
    plain = await api.post("/v1/auth/logout", json={"refresh_token": first["refresh_token"]})
    assert plain.json() == {"end_session_url": None}


@requires_db
@pytest.mark.asyncio
async def test_a_program_sends_the_providers_token(sso) -> None:  # noqa: ANN001
    api, idp = sso
    admin = await _admin(api)
    await _grant(api, admin, "oidc-client:loader", "sol_*", "create_tenants")
    token = idp.access_token(sub="svc-loader", azp="loader")
    me = (await api.get("/v1/me", headers=_auth(token))).json()
    assert (me["consumer"], me["kind"], me["via"], me["create_tenants"]) == (
        "loader", "service", "oidc", ["sol_*"],
    )
    # For another audience, or from another key: refused.
    other = idp.sign({"sub": "svc-loader", "aud": "another-api"})
    assert (await api.get("/v1/me", headers=_auth(other))).status_code == HTTPStatus.UNAUTHORIZED
    forged = FakeIdp().access_token(sub="svc-loader", azp="loader")
    assert (await api.get("/v1/me", headers=_auth(forged))).status_code == HTTPStatus.UNAUTHORIZED


@requires_db
@pytest.mark.asyncio
async def test_grants_name_provider_subjects(sso) -> None:  # noqa: ANN001
    api, _ = sso
    admin = await _admin(api)
    for subject in ("oidc-sub:u-1", "oidc-group:analysts", "oidc-role:r", "oidc-client:c"):
        await _grant(api, admin, subject, "acme", "read")
    for bad in ("oidc-group:", "group:analysts", "user:nobody"):
        resp = await api.post("/v1/grants", headers=_auth(admin),
                              json={"subject": bad, "tenant": "acme", "level": "read"})
        assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, bad

"""The sign-in routes (``/v1/auth/*``, ``/v1/me/password``) without a database.

Users, sessions and pending single sign-ons live in a scripted pool
(``_signin_fakes``); tokens are graphos's own, checked by the real
authenticator; the identity provider is ``fake_idp``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio

from graphos import identity, oidc
from graphos.app import create_app
from graphos.auth import TokenAuthenticator
from graphos.config import Settings
from graphos.identity import AccessStore, Throttle

from ._signin_fakes import FakePool, make_signer, user_row
from .fake_idp import AUDIENCE, CLIENT_ID, ISSUER, FakeIdp

PASSWORD = "right-password-1"
CALLBACK = "http://127.0.0.1:33418/callback"
UI_CALLBACK = "https://ui.example.com/auth/callback"


@pytest.fixture(autouse=True)
def _cheap_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity, "_SCRYPT_N", 2**4)


class Tables:
    """``users``, ``sessions``, ``oauth_*`` and ``oidc_pending`` in memory."""

    def __init__(self) -> None:
        self.users = {u["name"]: u for u in (
            user_row("alice"), user_row("graphos"), user_row("svc", kind="service"),
            user_row("newbie", must_change=True), user_row("gone", disabled=True),
        )}
        self.hashes = {name: identity.hash_password(PASSWORD) for name in self.users}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.closed: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.clients: dict[str, str] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.pool = FakePool()
        p = self.pool
        p.on("FROM graphos_meta.users;", lambda: list(self.users.values()))
        p.on("SELECT password_hash", self._password)
        p.on("SET password_hash", self._set_password)
        p.on("INSERT INTO graphos_meta.sessions", self._insert_session)
        p.on("FROM graphos_meta.sessions WHERE refresh_hash", self._session)
        p.on("WITH ended", self.closed.get)
        p.on("INSERT INTO graphos_meta.oidc_pending", self._insert_pending)
        p.on("DELETE FROM graphos_meta.oidc_pending WHERE state", self._take_pending)
        p.on("SELECT info::text FROM graphos_meta.oauth_clients", self.clients.get)
        p.on("INSERT INTO graphos_meta.oauth_codes", self._insert_code)

    def _password(self, name: str) -> dict[str, Any] | None:
        user = self.users.get(name)
        if user is None:
            return None
        return {"password_hash": self.hashes[name], "disabled": user["disabled"], "locked": False}

    def _set_password(self, name: str, hashed: str, must_change: bool) -> str:  # noqa: FBT001
        if name not in self.users:
            return "UPDATE 0"
        self.hashes[name] = hashed
        user = self.users[name]
        self.users[name] = {**user, "must_change": must_change,
                            "token_version": user["token_version"] + 1}
        return "UPDATE 1"

    def _insert_session(self, *args: Any) -> None:
        family, user, refresh_hash, _idle, until, kind, claims = args[:7]
        self.sessions[refresh_hash] = {
            "id": len(self.sessions) + 1, "family": family, "user_name": user,
            "rotated_at": None, "revoked_at": None, "kind": kind, "claims": claims,
            "id_token": args[7], "client": args[8], "expired": False,
            "absolute_expires_at": until, "just_rotated": None,
        }

    def _session(self, refresh_hash: str, _grace: float) -> dict[str, Any] | None:
        return self.sessions.get(refresh_hash)

    def _insert_pending(self, state: str, *rest: Any) -> None:
        nonce, verifier, redirect_uri, return_to, expires = rest
        self.pending[state] = {"nonce": nonce, "code_verifier": verifier,
                               "redirect_uri": redirect_uri, "return_to": return_to,
                               "expires": expires}

    def _take_pending(self, state: str) -> dict[str, Any] | None:
        row = self.pending.pop(state, None)
        if row is None:
            return None
        return {**row, "live": row["expires"] > datetime.now(UTC)}

    def _insert_code(self, code_hash: str, client: str, user: str, data: str, _ttl: int) -> None:
        self.codes[code_hash] = {"client": client, "user": user, "data": json.loads(data)}

    def session_of(self, token: str) -> dict[str, Any]:
        return self.sessions[hashlib.sha256(token.encode()).hexdigest()]


async def _app(
    *, idp: FakeIdp | None = None, **settings: Any,
) -> tuple[Any, Tables]:
    tables = Tables()
    app = create_app(Settings(db_password="x", auth_cookie_secure=True, **settings))
    store = AccessStore(tables.pool)  # type: ignore[arg-type]
    await store.load()
    signer = make_signer()
    provider = None
    if idp is not None:
        provider = oidc.Provider(issuer=ISSUER, client_id=CLIENT_ID, audience=AUDIENCE,
                                 transport=idp.transport())
    app.state.pool = tables.pool
    app.state.access_store = store
    app.state.signer = signer
    app.state.oidc = provider
    app.state.authenticator = TokenAuthenticator(signer, store, provider)
    app.state.login_throttle = Throttle(50)
    return app, tables


def _http(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://localhost")


@pytest_asyncio.fixture
async def setup() -> AsyncIterator[tuple[Any, Tables, httpx.AsyncClient]]:
    app, tables = await _app()
    async with _http(app) as http:
        yield app, tables, http


async def _login(http: httpx.AsyncClient, user: str = "alice", password: str = PASSWORD,
                 **extra: Any) -> httpx.Response:
    return await http.post("/v1/auth/login", json={"user": user, "password": password, **extra})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── Signing in ───────────────────────────────────────────────────────────


async def test_sign_in_is_not_found_where_authentication_is_off(setup: Any) -> None:
    app, _, http = setup
    app.state.access_store = None
    assert (await _login(http)).status_code == HTTPStatus.NOT_FOUND
    assert (await http.post("/v1/auth/refresh", json={})).status_code == HTTPStatus.NOT_FOUND


async def test_a_person_gets_an_access_token_and_a_session(setup: Any) -> None:
    app, tables, http = setup
    resp = await _login(http, user="  Alice ")
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["session"] is True and body["must_change"] is False
    assert body["expires_in"] == 900  # noqa: PLR2004
    claims = app.state.signer.verify(body["access_token"])
    assert claims["sub"] == "alice" and "ro" not in claims
    session = tables.session_of(body["refresh_token"])
    assert session["user_name"] == "alice" and session["client"] is None
    assert claims["sxp"] == int(session["absolute_expires_at"].timestamp())


async def test_a_service_account_gets_no_session(setup: Any) -> None:
    _, tables, http = setup
    body = (await _login(http, user="svc")).json()
    assert body["refresh_token"] is None and body["session"] is False
    assert not tables.sessions


async def test_a_browser_gets_its_refresh_token_in_a_strict_http_only_cookie(setup: Any) -> None:
    _, tables, http = setup
    resp = await _login(http, session_cookie=True)
    assert resp.json()["refresh_token"] is None and resp.json()["session"] is True
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith("graphos_session=")
    for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age=43200"):
        assert attribute.lower() in cookie.lower()
    token = cookie.split(";")[0].split("=", 1)[1]
    assert tables.session_of(token)["user_name"] == "alice"


async def test_a_password_that_must_change_is_said_so(setup: Any) -> None:
    _, _, http = setup
    assert (await _login(http, user="newbie")).json()["must_change"] is True


@pytest.mark.parametrize(
    ("user", "password"),
    [("alice", "wrong-password"), ("nobody", PASSWORD), ("gone", PASSWORD)],
)
async def test_a_refused_sign_in_answers_401_with_one_message(
    setup: Any, user: str, password: str,
) -> None:
    _, tables, http = setup
    resp = await _login(http, user=user, password=password)
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    assert resp.json()["detail"] == identity.REFUSED_MESSAGE
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    assert not tables.sessions


async def test_the_local_admin_cannot_sign_in_where_it_is_switched_off() -> None:
    app, tables = await _app(local_admin=False)
    async with _http(app) as http:
        resp = await _login(http, user="GRAPHOS")
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    assert resp.json()["detail"] == identity.REFUSED_MESSAGE
    assert not tables.pool.ran("SELECT password_hash")


async def test_too_many_sign_ins_from_one_address_answer_429(setup: Any) -> None:
    app, _, http = setup
    app.state.login_throttle = Throttle(2)
    for _ in range(2):
        await _login(http, password="wrong")
    resp = await _login(http)                         # even the right password
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert resp.headers["Retry-After"] == "60"


# ── Renewing ─────────────────────────────────────────────────────────────


async def test_a_refresh_token_renews_once_and_rotates(setup: Any) -> None:
    app, tables, http = setup
    first = (await _login(http)).json()
    resp = await http.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert resp.status_code == HTTPStatus.OK, resp.text
    renewed = resp.json()
    assert renewed["refresh_token"] and renewed["refresh_token"] != first["refresh_token"]
    assert app.state.signer.verify(renewed["access_token"])["sub"] == "alice"
    assert "set-cookie" not in resp.headers                   # it came in the body
    assert tables.pool.ran("SET rotated_at = now()")


async def test_without_a_refresh_token_renewal_is_refused(setup: Any) -> None:
    _, _, http = setup
    resp = await http.post("/v1/auth/refresh", json={})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    assert resp.json()["detail"] == "no session"
    unknown = await http.post("/v1/auth/refresh", json={"refresh_token": "made-up"})
    assert unknown.status_code == HTTPStatus.UNAUTHORIZED


async def test_a_session_cookie_renews_into_a_new_cookie(setup: Any) -> None:
    _, tables, http = setup
    await _login(http, session_cookie=True)
    old = http.cookies["graphos_session"]
    resp = await http.post("/v1/auth/refresh", json={})
    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["refresh_token"] is None
    new = http.cookies["graphos_session"]
    assert new != old and tables.session_of(new)["user_name"] == "alice"


async def test_a_disabled_or_deleted_users_session_does_not_renew(setup: Any) -> None:
    app, tables, http = setup
    token = (await _login(http)).json()["refresh_token"]
    tables.users["alice"] = user_row("alice", disabled=True)
    await app.state.access_store.load()
    resp = await http.post("/v1/auth/refresh", json={"refresh_token": token})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    token2 = (await _login(http, user="newbie")).json()["refresh_token"]
    del tables.users["newbie"]
    await app.state.access_store.load()
    resp = await http.post("/v1/auth/refresh", json={"refresh_token": token2})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


async def test_an_mcp_clients_session_does_not_renew_here(setup: Any) -> None:
    _, tables, http = setup
    token = (await _login(http)).json()["refresh_token"]
    tables.session_of(token)["client"] = "some-client"
    resp = await http.post("/v1/auth/refresh", json={"refresh_token": token})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    assert "/oauth/token" in resp.json()["detail"]
    assert not tables.pool.ran("SET rotated_at")


async def test_within_the_reuse_grace_a_cookie_session_renews_and_keeps_its_cookie(
    setup: Any,
) -> None:
    _, tables, http = setup
    await _login(http, session_cookie=True)
    cookie = http.cookies["graphos_session"]
    session = tables.session_of(cookie)
    session.update(rotated_at=datetime.now(UTC), just_rotated=True)
    tables.pool.rules.insert(0, ("SELECT EXISTS", True))
    resp = await http.post("/v1/auth/refresh", json={})
    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["session"] is True and "set-cookie" not in resp.headers


async def test_the_must_change_flag_is_carried_through_renewal(setup: Any) -> None:
    _, _, http = setup
    token = (await _login(http, user="newbie")).json()["refresh_token"]
    resp = await http.post("/v1/auth/refresh", json={"refresh_token": token})
    assert resp.json()["must_change"] is True


# ── Signing out ──────────────────────────────────────────────────────────


async def test_signing_out_ends_the_session_and_clears_the_cookie(setup: Any) -> None:
    _, tables, http = setup
    await _login(http, session_cookie=True)
    token = http.cookies["graphos_session"]
    tables.closed[hashlib.sha256(token.encode()).hexdigest()] = {
        "kind": "native", "family": "f", "id_token": None,
    }
    resp = await http.post("/v1/auth/logout", json={})
    assert resp.status_code == HTTPStatus.OK and resp.json() == {"end_session_url": None}
    assert 'graphos_session=""' in resp.headers["set-cookie"]
    assert "graphos_session" not in http.cookies
    assert tables.pool.ran("WITH ended")[0].args == (hashlib.sha256(token.encode()).hexdigest(),)


async def test_signing_out_without_a_session_still_answers(setup: Any) -> None:
    _, tables, http = setup
    resp = await http.post("/v1/auth/logout", json={})
    assert resp.status_code == HTTPStatus.OK and not tables.pool.ran("WITH ended")
    resp = await http.post("/v1/auth/logout", json={"refresh_token": "unknown"})
    assert resp.status_code == HTTPStatus.OK and resp.json() == {"end_session_url": None}


async def test_signing_out_of_a_single_sign_on_session_gives_the_providers_sign_out() -> None:
    app, tables = await _app(idp=FakeIdp())
    tables.closed[hashlib.sha256(b"tok").hexdigest()] = {
        "kind": "oidc", "family": "f", "id_token": "the-id-token",
    }
    async with _http(app) as http:
        resp = await http.post("/v1/auth/logout", json={
            "refresh_token": "tok", "post_logout_redirect_uri": "https://ui.example.com/",
        })
    url = resp.json()["end_session_url"]
    assert url.startswith(f"{ISSUER}/protocol/openid-connect/logout?")
    query = parse_qs(urlsplit(url).query)
    assert query["id_token_hint"] == ["the-id-token"]
    assert query["post_logout_redirect_uri"] == ["https://ui.example.com/"]


async def test_an_unreachable_provider_does_not_stop_signing_out() -> None:
    app, tables = await _app(idp=FakeIdp())

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    app.state.oidc = oidc.Provider(issuer=ISSUER, client_id=CLIENT_ID, audience=AUDIENCE,
                                   transport=httpx.MockTransport(down))
    tables.closed[hashlib.sha256(b"tok").hexdigest()] = {
        "kind": "oidc", "family": "f", "id_token": None,
    }
    async with _http(app) as http:
        resp = await http.post("/v1/auth/logout", json={"refresh_token": "tok"})
    assert resp.status_code == HTTPStatus.OK and resp.json() == {"end_session_url": None}


async def test_auth_config_names_the_provider_only_when_there_is_one(setup: Any) -> None:
    _, _, http = setup
    assert (await http.get("/v1/auth/config")).json() == {"password": True, "oidc": None}
    app, _ = await _app(idp=FakeIdp(), oidc_display_name="Acme SSO")
    async with _http(app) as other:
        config = (await other.get("/v1/auth/config")).json()
    assert config["oidc"] == {"issuer": ISSUER, "client_id": CLIENT_ID,
                              "scopes": "openid profile email", "display_name": "Acme SSO"}


# ── Changing a password ──────────────────────────────────────────────────


async def _change(http: httpx.AsyncClient, token: str, current: str, new: str,
                  **extra: Any) -> httpx.Response:
    return await http.post("/v1/me/password", headers=_bearer(token),
                           json={"current": current, "new": new, **extra})


async def test_changing_the_password_refuses_older_tokens_and_answers_new_ones(
    setup: Any,
) -> None:
    app, tables, http = setup
    old = (await _login(http, user="newbie")).json()["access_token"]
    resp = await _change(http, old, PASSWORD, "a-brand-new-password")
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["must_change"] is False and body["refresh_token"]
    assert identity.verify_password("a-brand-new-password", tables.hashes["newbie"])
    assert tables.pool.ran("UPDATE graphos_meta.sessions SET revoked_at")[0].args == ("newbie",)
    assert app.state.signer.verify(body["access_token"])["ver"] == 2  # noqa: PLR2004
    assert (await http.get("/v1/me", headers=_bearer(old))).status_code == (
        HTTPStatus.UNAUTHORIZED)
    assert (await http.get("/v1/me", headers=_bearer(body["access_token"]))).status_code == (
        HTTPStatus.OK)


async def test_a_password_that_must_change_allows_only_changing_it(setup: Any) -> None:
    _, _, http = setup
    token = (await _login(http, user="newbie")).json()["access_token"]
    resp = await http.get("/v1/auth/mcp/request", headers=_bearer(token), params={
        "client_id": "c", "redirect_uri": CALLBACK, "code_challenge": "c" * 43,
    })
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert "change your password first" in resp.json()["detail"]


async def test_a_wrong_current_password_is_403_and_changes_nothing(setup: Any) -> None:
    _, tables, http = setup
    token = (await _login(http)).json()["access_token"]
    before = tables.hashes["alice"]
    resp = await _change(http, token, "not-the-password", "a-brand-new-password")
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert resp.json()["detail"] == "the current password is wrong"
    assert tables.hashes["alice"] == before
    # It counts as a failed sign-in.
    assert tables.pool.ran("RETURNING failed_logins")


@pytest.mark.parametrize(
    ("new", "says"),
    [("short", "at least 12"), (PASSWORD, "differ")],
)
async def test_a_new_password_must_meet_the_policy(setup: Any, new: str, says: str) -> None:
    _, tables, http = setup
    token = (await _login(http)).json()["access_token"]
    before = tables.hashes["alice"]
    resp = await _change(http, token, PASSWORD, new)
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert says in resp.json()["detail"]
    assert tables.hashes["alice"] == before


async def test_the_new_password_cannot_be_the_user_name() -> None:
    app, tables = await _app(password_min_length=8)
    tables.users["longname"] = user_row("longname")
    tables.hashes["longname"] = identity.hash_password(PASSWORD)
    await app.state.access_store.load()
    async with _http(app) as http:
        token = (await _login(http, user="longname")).json()["access_token"]
        resp = await _change(http, token, PASSWORD, "LongName")
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "user name" in resp.json()["detail"]


async def test_a_new_password_can_go_to_the_cookie(setup: Any) -> None:
    _, _, http = setup
    token = (await _login(http)).json()["access_token"]
    resp = await _change(http, token, PASSWORD, "a-brand-new-password", session_cookie=True)
    assert resp.json()["refresh_token"] is None and "graphos_session" in http.cookies


async def test_an_mcp_clients_token_cannot_change_the_password(setup: Any) -> None:
    app, tables, http = setup
    user = app.state.access_store.user("alice")
    token, _ = app.state.signer.issue(user, read_only=True, session_until=int(time.time()) + 60)
    resp = await _change(http, token, PASSWORD, "a-brand-new-password")
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert not tables.pool.ran("SELECT password_hash")


async def test_someone_signed_in_at_the_provider_has_no_password_to_change() -> None:
    app, tables = await _app(idp=FakeIdp())
    who = oidc.Identity(subject="u-1", consumer="alice")
    token, _ = app.state.signer.issue_for(who)
    async with _http(app) as http:
        resp = await _change(http, token, PASSWORD, "a-brand-new-password")
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert not tables.pool.ran("SELECT password_hash")


@pytest.mark.parametrize("path", ["/v1/auth/login", "/v1/me/password"])
async def test_a_password_that_is_not_valid_unicode_is_a_422(setup: Any, path: str) -> None:
    _, _, http = setup
    token = (await _login(http)).json()["access_token"]
    escape = "\\" + "ud800"                       # the six characters, not the code point
    body = (f'{{"user": "alice", "password": "x{escape}y"}}' if path == "/v1/auth/login"
            else f'{{"current": "{PASSWORD}", "new": "abcdefghijkl{escape}mnop"}}')
    assert body.isascii()
    resp = await http.post(path, content=body, headers={
        **_bearer(token), "Content-Type": "application/json",
    })
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


# ── Answering an MCP client from the web UI ──────────────────────────────


def _register(tables: Tables, client_id: str = "c1") -> None:
    tables.clients[client_id] = json.dumps({
        "client_id": client_id, "client_name": "Acme Desktop", "redirect_uris": [CALLBACK],
        "token_endpoint_auth_method": "none",
    })


def _ui_token(app: Any, name: str = "alice", **extra: Any) -> str:
    user = app.state.access_store.user(name)
    extra.setdefault("session_until", int(time.time()) + 600)
    return app.state.signer.issue(user, **extra)[0]


CHALLENGE = "c" * 43


async def test_the_allow_card_learns_which_client_asks_and_where_the_answer_goes(
    setup: Any,
) -> None:
    app, tables, http = setup
    _register(tables)
    resp = await http.get("/v1/auth/mcp/request", headers=_bearer(_ui_token(app)), params={
        "client_id": "c1", "redirect_uri": CALLBACK, "code_challenge": CHALLENGE,
    })
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json() == {"client_id": "c1", "client_name": "Acme Desktop",
                           "redirect_host": "127.0.0.1:33418"}


@pytest.mark.parametrize(
    "params",
    [{"client_id": "unknown"}, {"redirect_uri": "http://127.0.0.1:1/other"}],
)
async def test_the_allow_card_refuses_a_request_authorize_would_not_pass(
    setup: Any, params: dict[str, str],
) -> None:
    app, tables, http = setup
    _register(tables)
    resp = await http.get("/v1/auth/mcp/request", headers=_bearer(_ui_token(app)), params={
        "client_id": "c1", "redirect_uri": CALLBACK, "code_challenge": CHALLENGE, **params,
    })
    assert resp.status_code == HTTPStatus.BAD_REQUEST


@pytest.mark.parametrize(
    ("name", "extra"),
    [("svc", {}), ("alice", {"read_only": True}), ("alice", {"session_until": None})],
    ids=["service-account", "mcp-token", "no-session"],
)
async def test_only_a_person_signed_in_to_a_session_answers_an_mcp_client(
    setup: Any, name: str, extra: dict[str, Any],
) -> None:
    app, tables, http = setup
    _register(tables)
    token = _ui_token(app, name, **extra)
    resp = await http.get("/v1/auth/mcp/request", headers=_bearer(token), params={
        "client_id": "c1", "redirect_uri": CALLBACK, "code_challenge": CHALLENGE,
    })
    assert resp.status_code == HTTPStatus.FORBIDDEN
    approve = await http.post("/v1/auth/mcp/approve", headers=_bearer(token), json={
        "client_id": "c1", "redirect_uri": CALLBACK, "code_challenge": CHALLENGE, "allow": True,
    })
    assert approve.status_code == HTTPStatus.FORBIDDEN and not tables.codes


async def test_the_local_admin_answers_no_mcp_client_where_it_is_switched_off() -> None:
    app, tables = await _app(local_admin=False)
    _register(tables)
    async with _http(app) as http:
        resp = await http.post("/v1/auth/mcp/approve", headers=_bearer(_ui_token(app, "graphos")),
                               json={"client_id": "c1", "redirect_uri": CALLBACK,
                                     "code_challenge": CHALLENGE, "allow": True})
    assert resp.status_code == HTTPStatus.FORBIDDEN and not tables.codes


async def test_allowing_gives_the_client_a_code_capped_by_the_approving_session(
    setup: Any,
) -> None:
    app, tables, http = setup
    _register(tables)
    until = int(time.time()) + 300
    resp = await http.post("/v1/auth/mcp/approve", headers=_bearer(_ui_token(
        app, session_until=until)), json={
        "client_id": "c1", "redirect_uri": CALLBACK, "code_challenge": CHALLENGE,
        "state": "st-9", "allow": True,
    })
    assert resp.status_code == HTTPStatus.OK, resp.text
    redirect = resp.json()["redirect"]
    query = parse_qs(urlsplit(redirect).query)
    assert redirect.startswith(CALLBACK) and query["state"] == ["st-9"]
    (code,) = tables.codes.values()
    assert code["user"] == "alice" and code["client"] == "c1"
    assert code["data"]["until"] == until and code["data"]["code_challenge"] == CHALLENGE


async def test_refusing_answers_access_denied_and_issues_no_code(setup: Any) -> None:
    app, tables, http = setup
    _register(tables)
    resp = await http.post("/v1/auth/mcp/approve", headers=_bearer(_ui_token(app)), json={
        "client_id": "c1", "redirect_uri": CALLBACK, "code_challenge": CHALLENGE,
        "state": "st-9", "allow": False,
    })
    assert parse_qs(urlsplit(resp.json()["redirect"]).query) == {
        "error": ["access_denied"], "state": ["st-9"],
    }
    assert not tables.codes


async def test_where_graphos_signs_no_mcp_client_in_the_allow_card_is_not_found(
    setup: Any,
) -> None:
    app, _, http = setup
    app.state.oauth = None
    resp = await http.get("/v1/auth/mcp/request", headers=_bearer(_ui_token(app)), params={
        "client_id": "c1", "redirect_uri": CALLBACK, "code_challenge": CHALLENGE,
    })
    assert resp.status_code == HTTPStatus.NOT_FOUND


# ── Single sign-on ───────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def sso() -> AsyncIterator[tuple[Any, Tables, FakeIdp, httpx.AsyncClient]]:
    idp = FakeIdp()
    app, tables = await _app(idp=idp, oidc_redirect_uris=[UI_CALLBACK])
    async with _http(app) as http:
        yield app, tables, idp, http


async def _start(http: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await http.post("/v1/auth/oidc/start", json={"redirect_uri": UI_CALLBACK, **body})


async def test_single_sign_on_is_not_found_without_a_provider(setup: Any) -> None:
    _, _, http = setup
    assert (await _start(http)).status_code == HTTPStatus.NOT_FOUND
    assert (await http.post("/v1/auth/oidc/callback", json={"state": "s"})).status_code == (
        HTTPStatus.NOT_FOUND)


async def test_starting_keeps_the_pkce_verifier_and_gives_the_providers_url(sso: Any) -> None:
    _, tables, _, http = sso
    resp = await _start(http, return_to="/acme/explore?x=1", silent=True)
    assert resp.status_code == HTTPStatus.OK, resp.text
    query = {k: v[0] for k, v in parse_qs(urlsplit(resp.json()["authorization_url"]).query).items()}
    pending = tables.pending[query["state"]]
    assert query["prompt"] == "none" and query["redirect_uri"] == UI_CALLBACK
    digest = hashlib.sha256(pending["code_verifier"].encode()).digest()
    assert query["code_challenge"] == base64.urlsafe_b64encode(digest).decode().rstrip("=")
    assert "code_verifier" not in query and query["nonce"] == pending["nonce"]
    assert pending["return_to"] == "/acme/explore?x=1"
    assert pending["expires"] <= datetime.now(UTC) + timedelta(seconds=oidc.PENDING_S)


@pytest.mark.parametrize(
    "uri",
    ["https://evil.example/auth/callback", "javascript:alert(1)", f"{UI_CALLBACK}/../x"],
)
async def test_starting_refuses_a_callback_graphos_does_not_accept(sso: Any, uri: str) -> None:
    _, tables, _, http = sso
    resp = await _start(http, redirect_uri=uri)
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY and not tables.pending


async def test_without_a_callback_list_only_web_schemes_are_accepted() -> None:
    app, tables = await _app(idp=FakeIdp())
    async with _http(app) as http:
        assert (await _start(http, redirect_uri="javascript:alert(1)")).status_code == (
            HTTPStatus.UNPROCESSABLE_ENTITY)
        assert (await _start(http, redirect_uri="https://ui.example.com/cb")).status_code == (
            HTTPStatus.OK)
    assert len(tables.pending) == 1


@pytest.mark.parametrize(
    "return_to", ["https://evil.example/", "//evil.example/", "evil", ""],
)
async def test_the_page_to_return_to_is_a_path_of_this_ui(sso: Any, return_to: str) -> None:
    _, tables, _, http = sso
    assert (await _start(http, return_to=return_to)).status_code == HTTPStatus.OK
    (pending,) = tables.pending.values()
    assert pending["return_to"] == "/"


@pytest.mark.parametrize("return_to", ["/\\evil.example/", "/\t/evil.example/"])
async def test_a_return_to_that_browsers_read_as_another_host_becomes_the_root(
    sso: Any, return_to: str,
) -> None:
    _, tables, _, http = sso
    await _start(http, return_to=return_to)
    (pending,) = tables.pending.values()
    assert pending["return_to"] == "/"


async def test_an_unreachable_provider_answers_502(sso: Any) -> None:
    app, _, _, http = sso

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    app.state.oidc = oidc.Provider(issuer=ISSUER, client_id=CLIENT_ID, audience=AUDIENCE,
                                   transport=httpx.MockTransport(down))
    resp = await _start(http)
    assert resp.status_code == HTTPStatus.BAD_GATEWAY


async def test_single_sign_on_starts_are_throttled(sso: Any) -> None:
    app, _, _, http = sso
    app.state.login_throttle = Throttle(1)
    await _start(http)
    assert (await _start(http)).status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert (await http.post("/v1/auth/oidc/callback", json={"state": "s"})).status_code == (
        HTTPStatus.TOO_MANY_REQUESTS)


async def test_the_callback_signs_in_once_and_opens_a_renewable_session(sso: Any) -> None:
    app, tables, idp, http = sso
    url = (await _start(http, return_to="/acme")).json()["authorization_url"]
    code, state = idp.approve(url, sub="u-42", preferred_username="alice")
    resp = await http.post("/v1/auth/oidc/callback", json={"state": state, "code": code})
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["return_to"] == "/acme" and body["session"] is True
    claims = app.state.signer.verify(body["access_token"])
    assert claims["sub"] == "oidc:u-42" and claims["idn"]["name"] == "alice"
    session = tables.session_of(body["refresh_token"])
    assert session["user_name"] == "oidc:u-42" and session["kind"] == "oidc"
    # The state is spent.
    again = await http.post("/v1/auth/oidc/callback", json={"state": state, "code": code})
    assert again.status_code == HTTPStatus.UNAUTHORIZED
    # The session renews from its stored identity.
    renewed = await http.post("/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert renewed.status_code == HTTPStatus.OK
    assert app.state.signer.verify(renewed.json()["access_token"])["sub"] == "oidc:u-42"


async def test_the_callback_can_put_the_session_in_the_cookie(sso: Any) -> None:
    _, _, idp, http = sso
    url = (await _start(http)).json()["authorization_url"]
    code, state = idp.approve(url)
    resp = await http.post("/v1/auth/oidc/callback", json={
        "state": state, "code": code, "session_cookie": True,
    })
    assert resp.json()["refresh_token"] is None and "graphos_session" in http.cookies


async def test_an_expired_sign_in_is_refused(sso: Any) -> None:
    _, tables, idp, http = sso
    url = (await _start(http)).json()["authorization_url"]
    code, state = idp.approve(url)
    tables.pending[state]["expires"] = datetime.now(UTC) - timedelta(seconds=1)
    resp = await http.post("/v1/auth/oidc/callback", json={"state": state, "code": code})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    assert "expired" in resp.json()["detail"] and not tables.sessions


async def test_a_provider_error_or_a_missing_code_is_refused(sso: Any) -> None:
    _, tables, _, http = sso
    for answer in ({"error": "login_required"}, {}):
        state = parse_qs(urlsplit((await _start(http)).json()["authorization_url"]).query)[
            "state"][0]
        resp = await http.post("/v1/auth/oidc/callback", json={"state": state, **answer})
        assert resp.status_code == HTTPStatus.UNAUTHORIZED
        assert "did not sign you in" in resp.json()["detail"]
    assert not tables.sessions and not tables.pending


async def test_a_code_the_provider_does_not_honour_is_refused(sso: Any) -> None:
    _, tables, _, http = sso
    state = parse_qs(urlsplit((await _start(http)).json()["authorization_url"]).query)["state"][0]
    resp = await http.post("/v1/auth/oidc/callback", json={"state": state, "code": "forged"})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    assert resp.json()["detail"] == "the identity provider's answer was not valid"
    assert not tables.sessions


async def test_an_id_token_with_another_nonce_is_refused(sso: Any) -> None:
    _, tables, idp, http = sso
    url = (await _start(http)).json()["authorization_url"]
    code, state = idp.approve(url, nonce="replayed")
    resp = await http.post("/v1/auth/oidc/callback", json={"state": state, "code": code})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED and not tables.sessions


async def test_an_unknown_state_is_refused(sso: Any) -> None:
    _, _, _, http = sso
    resp = await http.post("/v1/auth/oidc/callback", json={"state": "never-issued", "code": "c"})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


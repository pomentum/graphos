"""graphos as MCP clients' authorization server, end to end without a database.

A scripted pool keeps the clients, codes and sessions in memory
(``_signin_fakes``); the routes run in-process.  Covers registration, the
sign-in page, the authorization-code exchange (PKCE, redirect URI, single
use, expiry), renewal, revocation, and who may sign a client in.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import pytest_asyncio
from mcp.server.auth.provider import AccessToken, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull

from graphos import identity, oauth
from graphos.app import create_app
from graphos.auth import TokenAuthenticator
from graphos.config import Settings
from graphos.identity import AccessStore, Throttle

from ._signin_fakes import FakePool, make_signer, user_row

CALLBACK = "http://127.0.0.1:33418/callback"
PASSWORD = "right-password-1"


def _cheap_hash(password: str) -> str:
    salt = b"0123456789abcdef"
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16, r=8, p=1, dklen=32)
    b64 = base64.urlsafe_b64encode
    return f"scrypt$16$8$1${b64(salt).decode()}${b64(digest).decode()}"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.decode().rstrip("=")


class OAuthTables:
    """``oauth_clients``, ``oauth_codes``, ``sessions`` and ``users`` in memory."""

    def __init__(self, users: list[dict[str, Any]]) -> None:
        self.clients: dict[str, str] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.closed: list[str] = []
        self.users = {u["name"]: u for u in users}
        self.pool = FakePool()
        p = self.pool
        p.on("SELECT info::text FROM graphos_meta.oauth_clients", self.clients.get)
        p.on("INSERT INTO graphos_meta.oauth_clients", self.clients.__setitem__)
        p.on("SELECT count(*) FROM graphos_meta.oauth_clients", lambda: len(self.clients))
        p.on("INSERT INTO graphos_meta.oauth_codes", self._insert_code)
        p.on("SELECT user_name, data::text", self._load_code)
        p.on("DELETE FROM graphos_meta.oauth_codes WHERE code_hash", self._take_code)
        p.on("INSERT INTO graphos_meta.sessions", self._insert_session)
        p.on("FROM graphos_meta.sessions WHERE refresh_hash", self._session)
        p.on("WITH ended", self._close)
        p.on("SELECT password_hash", self._password)
        p.on("FROM graphos_meta.users;", lambda: list(self.users.values()))

    def _insert_code(self, code_hash: str, client: str, user: str, data: str, ttl: int) -> None:
        self.codes[code_hash] = {"client": client, "user": user, "data": data,
                                 "expires": time.time() + ttl}

    def _load_code(self, code_hash: str, client: str) -> dict[str, Any] | None:
        code = self.codes.get(code_hash)
        if code is None or code["client"] != client:
            return None
        return {"user_name": code["user"], "data": code["data"], "expires": code["expires"]}

    def _take_code(self, code_hash: str, client: str) -> dict[str, Any] | None:
        code = self.codes.get(code_hash)
        if code is None or code["client"] != client or code["expires"] < time.time():
            return None
        del self.codes[code_hash]
        return {"user_name": code["user"], "until": json.loads(code["data"])["until"]}

    def _insert_session(self, *args: Any) -> None:
        family, user, refresh_hash, _idle, until = args[:5]
        client = args[-1]
        self.sessions[refresh_hash] = {
            "id": len(self.sessions) + 1, "family": family, "user_name": user,
            "rotated_at": None, "revoked_at": None, "kind": "native", "claims": None,
            "id_token": None, "client": client, "expired": False,
            "absolute_expires_at": until, "just_rotated": None,
        }

    def _session(self, refresh_hash: str, _grace: float) -> dict[str, Any] | None:
        return self.sessions.get(refresh_hash)

    def _close(self, refresh_hash: str) -> dict[str, Any] | None:
        self.closed.append(refresh_hash)
        return {"kind": "native", "family": "f", "id_token": None}

    def _password(self, name: str) -> dict[str, Any] | None:
        user = self.users.get(name)
        if user is None:
            return None
        return {"password_hash": _cheap_hash(PASSWORD), "disabled": user["disabled"],
                "locked": False}

    @staticmethod
    def hashed(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()


USERS = [
    user_row("alice"), user_row("graphos"), user_row("svc", kind="service"),
    user_row("newbie", must_change=True), user_row("gone", disabled=True),
]


async def _app(*, local_admin: bool = True, per_min: int = 50) -> tuple[Any, OAuthTables]:
    tables = OAuthTables(USERS)
    settings = Settings(db_password="x", local_admin=local_admin)
    app = create_app(settings)
    store = AccessStore(tables.pool)  # type: ignore[arg-type]
    await store.load()
    signer = make_signer()
    app.state.pool = tables.pool
    app.state.access_store = store
    app.state.signer = signer
    app.state.authenticator = TokenAuthenticator(signer, store)
    app.state.login_throttle = Throttle(per_min)
    return app, tables


@pytest_asyncio.fixture
async def setup() -> AsyncIterator[tuple[Any, OAuthTables, httpx.AsyncClient]]:
    app, tables = await _app()
    async with _http(app) as http:
        yield app, tables, http


def _http(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost",
        follow_redirects=False,
    )


async def _register(http: httpx.AsyncClient, **extra: Any) -> dict[str, Any]:
    body = {"redirect_uris": [CALLBACK], "client_name": "Acme Desktop", **extra}
    resp = await http.post("/oauth/register", json=body)
    assert resp.status_code == HTTPStatus.CREATED, resp.text
    return resp.json()


def _request(client_id: str, challenge: str, **extra: str) -> dict[str, str]:
    return {"client_id": client_id, "redirect_uri": CALLBACK, "explicit": "1",
            "code_challenge": challenge, "state": "st-1", **extra}


async def _sign_in(
    http: httpx.AsyncClient, client_id: str, challenge: str, *, user: str = "alice",
    password: str = PASSWORD, action: str = "allow", **extra: str,
) -> httpx.Response:
    form = {**_request(client_id, challenge, **extra), "user": user, "password": password,
            "action": action}
    return await http.post("/oauth/login", data=form)


def _query(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


async def _code(http: httpx.AsyncClient, client_id: str, challenge: str) -> str:
    resp = await _sign_in(http, client_id, challenge)
    assert resp.status_code == HTTPStatus.FOUND, resp.text
    return _query(resp.headers["location"])["code"]


async def _exchange(http: httpx.AsyncClient, **form: str) -> httpx.Response:
    return await http.post("/oauth/token", data={"grant_type": "authorization_code", **form})


# ── Registration ─────────────────────────────────────────────────────────


async def test_a_client_registers_as_a_public_client_whatever_it_asks_for(setup: Any) -> None:
    _, tables, http = setup
    info = await _register(http, token_endpoint_auth_method="client_secret_post")
    assert info["token_endpoint_auth_method"] == "none" and "client_secret" not in info
    stored = json.loads(tables.clients[info["client_id"]])
    assert stored["token_endpoint_auth_method"] == "none" and "client_secret" not in stored


@pytest.mark.parametrize(
    "uris",
    [["https://app.example.com/cb"], ["http://localhost.evil.example/cb"],
     [CALLBACK, "http://evil.example/cb"], [f"http://127.0.0.1:{9000 + i}/cb" for i in range(6)]],
    ids=["web", "lookalike-host", "one-bad-among-good", "too-many"],
)
async def test_registration_refuses_redirects_other_than_a_few_loopback_ones(
    setup: Any, uris: list[str],
) -> None:
    _, tables, http = setup
    resp = await http.post("/oauth/register", json={"redirect_uris": uris})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.json()["error"] == "invalid_redirect_uri"
    assert not tables.clients


async def test_registration_refuses_oversized_fields(setup: Any) -> None:
    _, tables, http = setup
    resp = await http.post("/oauth/register", json={
        "redirect_uris": [CALLBACK], "client_name": "x" * (oauth.MAX_FIELD_CHARS + 1),
    })
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.json()["error"] == "invalid_client_metadata" and not tables.clients


async def test_registration_stops_when_the_server_keeps_as_many_clients_as_it_may(
    setup: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, tables, http = setup
    monkeypatch.setattr(oauth, "MAX_CLIENTS", 1)
    await _register(http)
    resp = await http.post("/oauth/register", json={"redirect_uris": [CALLBACK]})
    assert resp.status_code == HTTPStatus.BAD_REQUEST and len(tables.clients) == 1
    # Forgetting unused and idle clients comes first.
    assert tables.pool.ran("DELETE FROM graphos_meta.oauth_clients WHERE (last_used_at IS NULL")


async def test_registrations_from_one_address_are_throttled(
    setup: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, http = setup
    app.state.oauth.registrations = Throttle(per_min=1)
    await _register(http)
    resp = await http.post("/oauth/register", json={"redirect_uris": [CALLBACK]})
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert resp.headers["Retry-After"] == "60"


# ── Authorize and the sign-in page ───────────────────────────────────────


async def test_authorize_sends_the_browser_to_the_sign_in_page_with_the_request(
    setup: Any,
) -> None:
    _, _, http = setup
    client_id = (await _register(http))["client_id"]
    _, challenge = _pkce()
    resp = await http.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-1",
    })
    assert resp.status_code == HTTPStatus.FOUND
    location = resp.headers["location"]
    assert location.startswith("/oauth/login?")
    assert _query(location) == {
        "client_id": client_id, "redirect_uri": CALLBACK, "explicit": "1",
        "code_challenge": challenge, "state": "st-1",
    }
    page = await http.get(location)
    assert page.status_code == HTTPStatus.OK
    assert page.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert page.headers["Cache-Control"] == "no-store"
    assert "Acme Desktop" in page.text and "127.0.0.1:33418" in page.text


async def test_authorize_refuses_a_callback_the_client_did_not_register(setup: Any) -> None:
    _, _, http = setup
    client_id = (await _register(http))["client_id"]
    resp = await http.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id,
        "redirect_uri": "http://127.0.0.1:1/other", "code_challenge": _pkce()[1],
    })
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "location" not in resp.headers


async def test_the_sign_in_page_escapes_what_the_client_registered(setup: Any) -> None:
    _, _, http = setup
    client_id = (await _register(http, client_name='<script>alert(1)</script>"'))["client_id"]
    query = urlencode(_request(client_id, _pkce()[1], state='"><img src=x onerror=alert(1)>'))
    page = await http.get(f"/oauth/login?{query}")
    assert page.status_code == HTTPStatus.OK
    assert "<script>alert(1)" not in page.text and "&lt;script&gt;" in page.text
    assert "<img src=x" not in page.text


@pytest.mark.parametrize(
    "change",
    [{"client_id": "unknown"}, {"redirect_uri": "http://127.0.0.1:1/other"},
     {"code_challenge": ""}],
    ids=["unknown-client", "unregistered-callback", "no-pkce"],
)
async def test_the_sign_in_page_refuses_a_request_authorize_would_not_pass(
    setup: Any, change: dict[str, str],
) -> None:
    _, tables, http = setup
    client_id = (await _register(http))["client_id"]
    fields = {**_request(client_id, _pkce()[1]), **change}
    assert (await http.get(f"/oauth/login?{urlencode(fields)}")).status_code == (
        HTTPStatus.BAD_REQUEST)
    posted = await http.post("/oauth/login", data={
        **fields, "user": "alice", "password": PASSWORD, "action": "allow",
    })
    assert posted.status_code == HTTPStatus.BAD_REQUEST and not tables.codes


async def test_through_the_web_ui_the_sign_in_goes_to_its_authorize_screen(setup: Any) -> None:
    _, _, http = setup
    resp = await http.get("/oauth/login?client_id=c&state=s", headers={oauth.UI_MARK: "1"})
    assert resp.status_code == HTTPStatus.FOUND
    assert resp.headers["location"] == "/authorize?client_id=c&state=s"


async def test_cancelling_answers_access_denied_without_asking_for_a_password(
    setup: Any,
) -> None:
    _, tables, http = setup
    client_id = (await _register(http))["client_id"]
    resp = await _sign_in(http, client_id, _pkce()[1], password="", action="deny")
    assert resp.status_code == HTTPStatus.FOUND
    assert _query(resp.headers["location"]) == {"error": "access_denied", "state": "st-1"}
    assert resp.headers["location"].startswith(CALLBACK)
    assert not tables.codes


@pytest.mark.parametrize(
    ("user", "password", "status", "says"),
    [
        ("alice", "wrong-password", HTTPStatus.UNAUTHORIZED, "wrong user name or password"),
        ("nobody", PASSWORD, HTTPStatus.UNAUTHORIZED, "wrong user name or password"),
        ("gone", PASSWORD, HTTPStatus.UNAUTHORIZED, "wrong user name or password"),
        ("svc", PASSWORD, HTTPStatus.FORBIDDEN, "service account"),
        ("newbie", PASSWORD, HTTPStatus.FORBIDDEN, "Change your password"),
    ],
)
async def test_the_sign_in_page_refuses_who_may_not_sign_a_client_in(
    setup: Any, user: str, password: str, status: int, says: str,
) -> None:
    _, tables, http = setup
    client_id = (await _register(http))["client_id"]
    resp = await _sign_in(http, client_id, _pkce()[1], user=user, password=password)
    assert resp.status_code == status
    assert says in resp.text and 'role="alert"' in resp.text
    assert not tables.codes


async def test_the_local_admin_is_refused_where_it_is_switched_off() -> None:
    app, tables = await _app(local_admin=False)
    async with _http(app) as http:
        client_id = (await _register(http))["client_id"]
        resp = await _sign_in(http, client_id, _pkce()[1], user=" GRAPHOS ")
    assert resp.status_code == HTTPStatus.UNAUTHORIZED and not tables.codes
    assert not tables.pool.ran("SELECT password_hash")      # no password checked at all


async def test_the_sign_in_page_shares_the_sign_in_throttle() -> None:
    app, tables = await _app(per_min=1)
    async with _http(app) as http:
        client_id = (await _register(http))["client_id"]
        await _sign_in(http, client_id, _pkce()[1], password="wrong")
        resp = await _sign_in(http, client_id, _pkce()[1])
    assert resp.status_code == HTTPStatus.TOO_MANY_REQUESTS and not tables.codes


# ── The code exchange ────────────────────────────────────────────────────


async def test_a_signed_in_person_gets_a_single_use_code_for_read_only_tokens(setup: Any) -> None:
    app, tables, http = setup
    client_id = (await _register(http))["client_id"]
    verifier, challenge = _pkce()
    resp = await _sign_in(http, client_id, challenge)
    query = _query(resp.headers["location"])
    assert query["state"] == "st-1"
    code = query["code"]
    assert list(tables.codes) == [tables.hashed(code)]       # only its hash is kept

    form = {"code": code, "redirect_uri": CALLBACK, "client_id": client_id,
            "code_verifier": verifier}
    tokens = await _exchange(http, **form)
    assert tokens.status_code == HTTPStatus.OK, tokens.text
    body = tokens.json()
    assert body["refresh_token"].startswith(f"{client_id}.")
    claims = app.state.signer.verify(body["access_token"])
    assert claims["sub"] == "alice" and claims["ro"] is True and isinstance(claims["sxp"], int)
    # The session belongs to the client.
    (session,) = tables.sessions.values()
    assert session["client"] == client_id

    again = await _exchange(http, **form)
    assert again.status_code == HTTPStatus.BAD_REQUEST
    assert again.json()["error"] == "invalid_grant"


async def test_a_wrong_pkce_verifier_is_refused(setup: Any) -> None:
    _, tables, http = setup
    client_id = (await _register(http))["client_id"]
    _, challenge = _pkce()
    code = await _code(http, client_id, challenge)
    resp = await _exchange(http, code=code, redirect_uri=CALLBACK, client_id=client_id,
                           code_verifier=_pkce()[0])
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.json()["error"] == "invalid_grant" and not tables.sessions


async def test_the_redirect_uri_must_be_the_one_the_code_was_issued_for(setup: Any) -> None:
    _, tables, http = setup
    client_id = (await _register(http, redirect_uris=[CALLBACK, "http://127.0.0.1:1/other"]))[
        "client_id"]
    verifier, challenge = _pkce()
    code = await _code(http, client_id, challenge)
    for uri in ("http://127.0.0.1:1/other", None):
        form = {"code": code, "client_id": client_id, "code_verifier": verifier}
        if uri:
            form["redirect_uri"] = uri
        resp = await _exchange(http, **form)
        assert resp.status_code == HTTPStatus.BAD_REQUEST
        assert resp.json()["error"] == "invalid_request"
    assert not tables.sessions


async def test_a_code_is_only_good_for_the_client_it_was_issued_to(setup: Any) -> None:
    _, tables, http = setup
    first = (await _register(http))["client_id"]
    second = (await _register(http))["client_id"]
    verifier, challenge = _pkce()
    code = await _code(http, first, challenge)
    resp = await _exchange(http, code=code, redirect_uri=CALLBACK, client_id=second,
                           code_verifier=verifier)
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.json()["error"] == "invalid_grant" and not tables.sessions
    # Still usable by its own client.
    ok = await _exchange(http, code=code, redirect_uri=CALLBACK, client_id=first,
                         code_verifier=verifier)
    assert ok.status_code == HTTPStatus.OK


async def test_an_expired_code_is_refused(setup: Any) -> None:
    _, tables, http = setup
    client_id = (await _register(http))["client_id"]
    verifier, challenge = _pkce()
    code = await _code(http, client_id, challenge)
    tables.codes[tables.hashed(code)]["expires"] = time.time() - 1
    resp = await _exchange(http, code=code, redirect_uri=CALLBACK, client_id=client_id,
                           code_verifier=verifier)
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.json()["error"] == "invalid_grant" and not tables.sessions


async def test_a_code_is_refused_when_its_person_may_no_longer_sign_in(setup: Any) -> None:
    app, tables, http = setup
    client_id = (await _register(http))["client_id"]
    verifier, challenge = _pkce()
    code = await _code(http, client_id, challenge)
    tables.users["alice"] = user_row("alice", disabled=True)
    await app.state.access_store.load()
    resp = await _exchange(http, code=code, redirect_uri=CALLBACK, client_id=client_id,
                           code_verifier=verifier)
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.json()["error"] == "invalid_grant" and not tables.sessions
    assert not tables.codes                                   # spent all the same


async def test_a_code_from_the_web_ui_opens_a_session_no_longer_than_the_one_that_allowed_it(
) -> None:
    app, tables = await _app()
    async with _http(app) as http:
        client_id = (await _register(http))["client_id"]
    provider: oauth.GraphosOAuth = app.state.oauth
    cap = datetime.now(UTC) + timedelta(minutes=7)
    code = await provider.issue_code(
        {**_request(client_id, "c" * 43), "client_id": client_id}, "alice", cap,
    )
    client = await provider.get_client(client_id)
    assert client is not None
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None and loaded.subject == "alice"
    tokens = await provider.exchange_authorization_code(client, loaded)
    claims = app.state.signer.verify(tokens.access_token)
    assert claims["sxp"] == int(cap.timestamp())


# ── Renewal and revocation ───────────────────────────────────────────────


async def _signed_in(http: httpx.AsyncClient) -> tuple[str, dict[str, Any]]:
    client_id = (await _register(http))["client_id"]
    verifier, challenge = _pkce()
    code = await _code(http, client_id, challenge)
    resp = await _exchange(http, code=code, redirect_uri=CALLBACK, client_id=client_id,
                           code_verifier=verifier)
    return client_id, resp.json()


async def test_a_client_renews_its_tokens_and_gets_a_new_refresh_token(setup: Any) -> None:
    app, tables, http = setup
    client_id, tokens = await _signed_in(http)
    resp = await http.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": client_id,
    })
    assert resp.status_code == HTTPStatus.OK, resp.text
    renewed = resp.json()
    assert renewed["refresh_token"].startswith(f"{client_id}.")
    assert renewed["refresh_token"] != tokens["refresh_token"]
    assert app.state.signer.verify(renewed["access_token"])["ro"] is True
    assert tables.pool.ran("SET last_used_at = now()")


async def test_another_client_cannot_renew_a_clients_refresh_token(setup: Any) -> None:
    _, tables, http = setup
    _, tokens = await _signed_in(http)
    thief = (await _register(http))["client_id"]
    for presented in (tokens["refresh_token"], f"{thief}.{tokens['refresh_token'].split('.')[1]}"):
        resp = await http.post("/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": presented, "client_id": thief,
        })
        assert resp.status_code == HTTPStatus.BAD_REQUEST
        assert resp.json()["error"] == "invalid_grant"
    assert not tables.pool.ran("SET rotated_at")


async def test_a_session_error_on_renewal_is_an_invalid_grant(setup: Any) -> None:
    _, tables, http = setup
    client_id, tokens = await _signed_in(http)
    for session in tables.sessions.values():
        session["expired"] = True
    resp = await http.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": client_id,
    })
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.json() == {"error": "invalid_grant", "error_description": "session ended"}


async def test_within_the_reuse_grace_the_client_keeps_its_refresh_token() -> None:
    app, tables = await _app()
    async with _http(app) as http:
        client_id = (await _register(http))["client_id"]
    client = await app.state.oauth.get_client(client_id)
    tables.pool.rules.insert(0, ("FROM graphos_meta.sessions WHERE refresh_hash", {
        "id": 1, "family": "f", "user_name": "alice", "rotated_at": datetime.now(UTC),
        "revoked_at": None, "kind": "native", "claims": None, "id_token": None,
        "client": client_id, "expired": False, "just_rotated": True,
        "absolute_expires_at": datetime.now(UTC) + timedelta(hours=1),
    }))
    tables.pool.rules.insert(0, ("SELECT EXISTS", True))
    presented = RefreshToken(token=f"{client_id}.tok", client_id=client_id, scopes=[])
    tokens = await app.state.oauth.exchange_refresh_token(client, presented, [])
    assert tokens.refresh_token == f"{client_id}.tok"


async def test_revoking_a_refresh_token_ends_its_session(setup: Any) -> None:
    _, tables, http = setup
    client_id, tokens = await _signed_in(http)
    resp = await http.post("/oauth/revoke", data={
        "client_id": client_id, "token": tokens["refresh_token"],
    })
    assert resp.status_code == HTTPStatus.OK
    assert tables.closed == [tables.hashed(tokens["refresh_token"].split(".", 1)[1])]


async def test_revoking_another_clients_token_or_an_access_token_changes_nothing(
    setup: Any,
) -> None:
    _, tables, http = setup
    _, tokens = await _signed_in(http)
    other = (await _register(http))["client_id"]
    for token in (tokens["refresh_token"], tokens["access_token"], "garbage"):
        resp = await http.post("/oauth/revoke", data={"client_id": other, "token": token})
        assert resp.status_code == HTTPStatus.OK
    assert not tables.closed


async def test_revoking_needs_a_known_client(setup: Any) -> None:
    _, tables, http = setup
    resp = await http.post("/oauth/revoke", data={"client_id": "nobody", "token": "x.y"})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    assert resp.json()["error"] == "unauthorized_client" and not tables.closed
    assert (await http.post("/oauth/revoke", data={"token": "x.y"})).status_code == (
        HTTPStatus.UNAUTHORIZED)


# ── The provider, directly ───────────────────────────────────────────────


def _provider(**state: Any) -> oauth.GraphosOAuth:
    defaults: dict[str, Any] = {"settings": Settings(db_password="x"), "signer": make_signer()}
    return oauth.GraphosOAuth(SimpleNamespace(state=SimpleNamespace(**{**defaults, **state})))


def _client(client_id: str = "c1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(client_id=client_id, redirect_uris=[CALLBACK])


async def test_a_refresh_token_names_its_client() -> None:
    provider = _provider()
    assert await provider.load_refresh_token(_client(), "no-dot") is None
    assert await provider.load_refresh_token(_client(), "c2.secret") is None
    loaded = await provider.load_refresh_token(_client(), "c1.secret")
    assert loaded is not None and loaded.client_id == "c1"


async def test_the_identity_assertion_grant_is_not_offered() -> None:
    with pytest.raises(TokenError) as refused:
        await _provider().exchange_identity_assertion(_client(), None)  # type: ignore[arg-type]
    assert refused.value.error == "unsupported_grant_type"


async def test_only_graphos_tokens_load_as_access_tokens() -> None:
    signer = make_signer()
    provider = _provider(signer=signer)
    token, _ = signer.issue(identity.User(**user_row("alice")))
    loaded = await provider.load_access_token(token)
    assert loaded is not None and loaded.subject == "alice"
    assert await provider.load_access_token(make_signer().issue(
        identity.User(**user_row("alice")))[0]) is None
    assert await provider.load_access_token("garbage") is None


async def test_revoking_an_access_token_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []

    async def close(_pool: object, token: str) -> None:
        closed.append(token)

    monkeypatch.setattr(identity, "close_session", close)
    provider = _provider(pool=FakePool())
    await provider.revoke_token(AccessToken(token="t", client_id="c1", scopes=[]))
    await provider.revoke_token(RefreshToken(token="c1.secret.with.dots", client_id="c1",
                                             scopes=[]))
    assert closed == ["secret.with.dots"]


@pytest.mark.parametrize(
    ("row", "local_admin"),
    [(None, True), (user_row("alice", disabled=True), True),
     (user_row("alice", kind="service"), True), (user_row("alice", must_change=True), True),
     (user_row("graphos"), False)],
    ids=["gone", "disabled", "service", "must-change", "local-admin-off"],
)
def test_tokens_go_only_to_a_person_who_may_sign_in(
    row: dict[str, Any] | None, local_admin: bool,  # noqa: FBT001
) -> None:
    store = SimpleNamespace(user=lambda _name: identity.User(**row) if row else None)
    provider = _provider(access_store=store,
                         settings=Settings(db_password="x", local_admin=local_admin))
    with pytest.raises(TokenError) as refused:
        provider._person("alice")
    assert refused.value.error == "invalid_grant"


def test_the_metadata_names_the_host_the_caller_reached() -> None:
    request = SimpleNamespace(url=SimpleNamespace(scheme="https", netloc="graph.example.org:8443"))
    meta = oauth.metadata(request)  # type: ignore[arg-type]
    assert meta["issuer"] == "https://graph.example.org:8443"
    assert meta["revocation_endpoint"] == "https://graph.example.org:8443/oauth/revoke"
    assert meta["revocation_endpoint_auth_methods_supported"] == ["none"]

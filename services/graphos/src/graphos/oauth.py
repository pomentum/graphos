"""graphos as the OAuth authorization server of MCP clients (ADR-0008).

Where graphos has no single sign-on, an MCP client (desktop, command line,
web) signs a person in here instead of asking for a pasted token: the
endpoint's protected-resource metadata names graphos itself as the
authorization server, the client registers (RFC 7591), sends the person's
browser to ``/oauth/authorize``, and exchanges the code it gets back for
graphos's own tokens.  With single sign-on the identity provider does all of
this and none of it is mounted.

**What the MCP SDK does.**  Its handlers parse and check the requests:
PKCE (S256 only), the client and its registered redirect URIs, the code's
expiry, client authentication.  This module stores clients and codes, shows
the sign-in page, and issues the tokens.

**Clients** are public ones whatever they ask for (no secret: PKCE binds a
code to the client that asked for it), so no client secret is kept.  A
redirect URI must be a loopback one (any port: desktop and command-line
clients listen there) or listed in ``GRAPHOS_MCP_REDIRECT_URIS``; at most
:data:`MAX_CLIENTS` register.

**The sign-in page** takes a person's name and password, says which client
asks and where the answer goes, and is never framed.  It counts towards the
same throttle and lock-out as ``/v1/auth/login``.  The authorization request
travels in the page and is checked again on submit, so it needs no storage.

**Codes** are 256 random bits, kept hashed, single use, :data:`CODE_TTL_S`.
**Tokens** are graphos's own: the access token of ``/v1/auth/login``, and a
graphos session as the refresh token (rotation, reuse detection, expiry,
revocation), written ``<client_id>.<session token>`` so it renews only for
the client it was issued to.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import secrets
from typing import Any
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE, RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, request_response
from starlette.types import ASGIApp

from graphos import identity

log = logging.getLogger(__name__)

CODE_TTL_S = 300
MAX_CLIENTS = 1_000
LOOPBACK = ("localhost", "127.0.0.1", "::1")
_PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                               "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


def _hash(value: str) -> str:
    # 256 random bits: a plain hash keeps the stored value useless to a reader.
    return hashlib.sha256(value.encode()).hexdigest()


def origin(request: Request) -> str:
    """The scheme and host the caller reached graphos at (``GRAPHOS_ALLOWED_HOSTS``
    bounds the host; uvicorn takes the scheme from a trusted proxy)."""
    return f"{request.url.scheme}://{request.url.netloc}"


def redirect_allowed(uri: str, listed: list[str]) -> bool:
    """Whether an MCP client may register ``uri``: a loopback ``http`` one, or listed."""
    if uri in listed:
        return True
    parts = urlsplit(uri)
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK


class GraphosOAuth:
    """The SDK's authorization-server provider, on graphos's tables and tokens.

    Reads the pool, the access store and the signer from ``app.state`` at each
    call: they are set at start, after the routes are mounted."""

    def __init__(self, app: Any) -> None:
        self.app = app

    @property
    def _state(self) -> Any:
        return self.app.state

    # ── Clients ─────────────────────────────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = await self._state.pool.fetchval(
            "SELECT info::text FROM graphos_meta.oauth_clients WHERE client_id = $1;", client_id,
        )
        return OAuthClientInformationFull.model_validate_json(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        listed = self._state.settings.mcp_redirect_uris
        uris = [str(u) for u in client_info.redirect_uris or []]
        refused = [u for u in uris if not redirect_allowed(u, listed)]
        if not uris or refused:
            raise RegistrationError(
                "invalid_redirect_uri",
                f"not a loopback redirect URI, nor one in GRAPHOS_MCP_REDIRECT_URIS: "
                f"{', '.join(refused) or '(none)'}",
            )
        # A public client: the SDK's answer echoes this object, so the client
        # learns it has no secret.
        client_info.token_endpoint_auth_method = "none"
        client_info.client_secret = None
        client_info.client_secret_expires_at = None
        async with self._state.pool.acquire() as conn, conn.transaction():
            await conn.execute("LOCK TABLE graphos_meta.oauth_clients IN EXCLUSIVE MODE;")
            if await conn.fetchval("SELECT count(*) FROM graphos_meta.oauth_clients;") >= (
                MAX_CLIENTS
            ):
                raise RegistrationError(
                    "invalid_client_metadata", "this graphos holds as many clients as it keeps",
                )
            await conn.execute(
                "INSERT INTO graphos_meta.oauth_clients (client_id, info) VALUES ($1, $2::jsonb);",
                client_info.client_id, client_info.model_dump_json(exclude_none=True),
            )
        log.info("oauth client registered", extra={
            "client": client_info.client_id, "client_name": client_info.client_name,
        })

    # ── Authorization ───────────────────────────────────────────────────

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams,
    ) -> str:
        """Where to send the browser: the sign-in page, carrying the request."""
        return "/oauth/login?" + urlencode(_drop_none({
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "explicit": "1" if params.redirect_uri_provided_explicitly else "0",
            "code_challenge": params.code_challenge,
            "state": params.state,
            "scope": " ".join(params.scopes or []) or None,
            "resource": params.resource,
        }))

    async def issue_code(self, request: dict[str, str], user: str) -> str:
        code = secrets.token_urlsafe(32)
        data = {k: request.get(k) for k in (
            "redirect_uri", "explicit", "code_challenge", "scope", "resource")}
        async with self._state.pool.acquire() as conn:
            await conn.execute("DELETE FROM graphos_meta.oauth_codes WHERE expires_at < now();")
            await conn.execute(
                "INSERT INTO graphos_meta.oauth_codes (code_hash, client_id, user_name, data, "
                "expires_at) VALUES ($1, $2, $3, $4::jsonb, now() + make_interval(secs => $5));",
                _hash(code), request["client_id"], user, json.dumps(data), CODE_TTL_S,
            )
        return code

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str,
    ) -> AuthorizationCode | None:
        row = await self._state.pool.fetchrow(
            "SELECT user_name, data::text AS data, extract(epoch FROM expires_at) AS expires "
            "FROM graphos_meta.oauth_codes WHERE code_hash = $1 AND client_id = $2;",
            _hash(authorization_code), client.client_id,
        )
        if row is None:
            return None
        data = json.loads(row["data"])
        return AuthorizationCode(
            code=authorization_code, scopes=(data.get("scope") or "").split(),
            expires_at=float(row["expires"]), client_id=client.client_id or "",
            code_challenge=data["code_challenge"], redirect_uri=AnyUrl(data["redirect_uri"]),
            redirect_uri_provided_explicitly=data.get("explicit") == "1",
            resource=data.get("resource"), subject=row["user_name"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        # Deleting is the single use: of two exchanges at once, one gets the row.
        user_name = await self._state.pool.fetchval(
            "DELETE FROM graphos_meta.oauth_codes WHERE code_hash = $1 AND client_id = $2 "
            "AND expires_at >= now() RETURNING user_name;",
            _hash(authorization_code.code), client.client_id,
        )
        if user_name is None:
            raise TokenError("invalid_grant", "the code was used, or has expired")
        user = self._person(user_name)
        settings = self._state.settings
        session = await identity.open_session(
            self._state.pool, user.name, idle_s=settings.session_idle_s,
            max_s=settings.session_max_s,
        )
        return self._tokens(user, f"{client.client_id}.{session}")

    # ── Refresh and revocation ──────────────────────────────────────────

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str,
    ) -> RefreshToken | None:
        client_id, dot, _ = refresh_token.partition(".")
        if not dot or client_id != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=client_id, scopes=[])

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str],
    ) -> OAuthToken:
        session = refresh_token.token.partition(".")[2]
        try:
            renewed = await identity.rotate_session(
                self._state.pool, session, idle_s=self._state.settings.session_idle_s,
            )
        except identity.SessionError as exc:
            raise TokenError("invalid_grant", str(exc)) from None
        user = self._person(renewed.user)
        # Within the reuse grace a session renews without rotating: keep the token.
        fresh = f"{client.client_id}.{renewed.refresh_token}" if renewed.refresh_token else None
        return self._tokens(user, fresh or refresh_token.token)

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            claims = self._state.signer.verify(token)
        except identity.TokenError:
            return None
        return AccessToken(
            token=token, client_id="graphos", scopes=[], expires_at=int(claims["exp"]),
            subject=str(claims["sub"]),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # An access token lives 15 minutes and is not stored: ending the session
        # behind a refresh token is what revocation can do.
        if isinstance(token, RefreshToken):
            await identity.close_session(self._state.pool, token.token.partition(".")[2])

    # ── Helpers ─────────────────────────────────────────────────────────

    def _person(self, name: str) -> identity.User:
        user = self._state.access_store.user(name)
        if user is None or user.disabled or user.kind != "person" or user.must_change:
            raise TokenError("invalid_grant", "this user may not sign in here")
        return user

    def _tokens(self, user: identity.User, refresh: str) -> OAuthToken:
        access, ttl = self._state.signer.issue(user)
        return OAuthToken(access_token=access, expires_in=ttl, refresh_token=refresh)


def _drop_none(d: dict[str, str | None]) -> dict[str, str]:
    return {k: v for k, v in d.items() if v is not None}


# ── The sign-in page ────────────────────────────────────────────────────────

_FIELDS = ("client_id", "redirect_uri", "explicit", "code_challenge", "state", "scope", "resource")


def _page(client: OAuthClientInformationFull, request: dict[str, str], error: str = "",
          status: int = 200) -> HTMLResponse:
    e = html.escape
    who = e(client.client_name or client.client_id or "An MCP client")
    to = e(urlsplit(request["redirect_uri"]).netloc or request["redirect_uri"])
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{e(request[k])}">' for k in _FIELDS if k in request
    )
    notice = f'<p class="err">{e(error)}</p>' if error else ""
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in to graphos</title>
<style>body{{font:15px system-ui,sans-serif;max-width:24rem;margin:4rem auto;padding:0 1rem;
color:#1b1f24;background:#f6f7f9}}h1{{font-size:1.3rem}}
label{{display:block;margin:.8rem 0 .2rem}}
input[type=text],input[type=password]{{width:100%;padding:.5rem;box-sizing:border-box}}
button{{margin:1rem .5rem 0 0;padding:.5rem 1rem}}.err{{color:#b42318}}.who{{font-weight:600}}
@media (prefers-color-scheme:dark){{body{{color:#e6e8eb;background:#14171a}}}}</style></head><body>
<h1>Sign in to graphos</h1>
<p><span class="who">{who}</span> asks to read graphos as you, with your grants. graphos then
sends you back to <span class="who">{to}</span>.</p>{notice}
<form method="post" action="/oauth/login">{hidden}
<label for="user">User</label><input id="user" name="user" type="text" autocomplete="username"
 required autofocus>
<label for="password">Password</label><input id="password" name="password" type="password"
 autocomplete="current-password" required>
<button name="action" value="allow" type="submit">Sign in and allow</button>
<button name="action" value="deny" type="submit" formnovalidate>Cancel</button>
</form></body></html>"""
    return HTMLResponse(body, status_code=status, headers=_PAGE_HEADERS)


def _problem(text: str, status: int = 400) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><title>graphos</title><p>{html.escape(text)}</p>",
        status_code=status, headers=_PAGE_HEADERS,
    )


async def _checked(provider: GraphosOAuth, request: dict[str, str]) -> (
        OAuthClientInformationFull | None):
    """The client, when the request is one ``/oauth/authorize`` would accept."""
    client = await provider.get_client(request.get("client_id", ""))
    if client is None or not request.get("code_challenge"):
        return None
    if request.get("redirect_uri") not in {str(u) for u in client.redirect_uris or []}:
        return None
    return client


_INVALID_LINK = "This sign-in link is not valid: start again from your MCP client."


async def _sign_in(request: Request, form: Any) -> identity.User | tuple[str, int]:
    """The person signing in, or why not and the status to answer with."""
    state = request.app.state
    settings = state.settings
    if not state.login_throttle.admit(request.client.host if request.client else "unknown"):
        return "Too many sign-in attempts: wait a minute.", 429
    name = str(form.get("user", "")).strip().lower()
    try:
        if name == identity.ADMIN_USER and not settings.local_admin:
            raise identity.AuthError(identity.REFUSED_MESSAGE)
        user: identity.User = await state.access_store.check_password(
            name, str(form.get("password", "")),
            max_failures=settings.login_max_failures, lockout_s=settings.login_lockout_s,
        )
    except identity.AuthError as exc:
        return str(exc), 401
    if user.kind != "person":
        return "A service account signs in with /v1/auth/login.", 403
    if user.must_change:
        return "Change your password in the web UI first.", 403
    return user


def login_endpoint(provider: GraphosOAuth) -> Any:
    async def login(request: Request) -> Response:
        if request.method == "GET":
            fields = {k: v for k, v in request.query_params.items() if k in _FIELDS}
            client = await _checked(provider, fields)
            return _page(client, fields) if client else _problem(_INVALID_LINK)
        form = await request.form()
        fields = {k: str(v) for k, v in form.items() if k in _FIELDS}
        client = await _checked(provider, fields)
        if client is None:
            return _problem(_INVALID_LINK)
        back, state = fields["redirect_uri"], fields.get("state")
        if form.get("action") != "allow":
            return RedirectResponse(
                construct_redirect_uri(back, error="access_denied", state=state), status_code=302,
            )
        user = await _sign_in(request, form)
        if isinstance(user, tuple):
            return _page(client, fields, *user)
        code = await provider.issue_code(fields, user.name)
        log.info("signed in", extra={
            "user": user.name, "kind": user.kind, "oauth_client": client.client_id,
        })
        return RedirectResponse(
            construct_redirect_uri(back, code=code, state=state), status_code=302,
        )

    return login


def metadata(request: Request) -> dict[str, Any]:
    """RFC 8414 metadata, for the URL the caller reached graphos at."""
    base = origin(request)
    meta = build_metadata(
        AnyHttpUrl(base), None, ClientRegistrationOptions(enabled=True),
        RevocationOptions(enabled=True),
    )
    meta.authorization_endpoint = AnyHttpUrl(f"{base}/oauth/authorize")
    meta.token_endpoint = AnyHttpUrl(f"{base}/oauth/token")
    meta.registration_endpoint = AnyHttpUrl(f"{base}/oauth/register")
    meta.revocation_endpoint = AnyHttpUrl(f"{base}/oauth/revoke")
    meta.token_endpoint_auth_methods_supported = ["none"]
    meta.revocation_endpoint_auth_methods_supported = ["none"]
    out = meta.model_dump(mode="json", exclude_none=True)
    # RFC 8414 §3.3: the issuer is compared as a string with what the resource's
    # metadata names; a URL type would add a trailing slash.
    out["issuer"] = base
    return out


def revoke_endpoint(provider: GraphosOAuth, clients: ClientAuthenticator) -> Any:
    """RFC 7009: ends the session behind a refresh token; always 200 for a known
    client.  The SDK's own handler wants a ``client_secret`` field even from a
    public client, which has none."""

    async def revoke(request: Request) -> Response:
        try:
            client = await clients.authenticate_request(request)
        except AuthenticationError as exc:
            return JSONResponse(
                {"error": "unauthorized_client", "error_description": exc.message},
                status_code=401,
            )
        token = str((await request.form()).get("token", ""))
        refresh = await provider.load_refresh_token(client, token)
        if refresh is not None:
            await provider.revoke_token(refresh)
        return Response(status_code=200, headers={"Cache-Control": "no-store"})

    return revoke


def _api(handler: Any, methods: list[str], *, cors: bool) -> ASGIApp:
    app: ASGIApp = RequestBodyLimitMiddleware(
        request_response(handler), DEFAULT_MAX_REQUEST_BODY_SIZE,
    )
    if cors:
        # Browser-based MCP clients call these with fetch; they carry no cookie.
        app = CORSMiddleware(app, allow_origins="*", allow_methods=methods)
    return app


def routes(app: Any) -> list[Route]:
    """The authorization server's routes, under ``/oauth/`` and its metadata."""
    provider = GraphosOAuth(app)
    clients = ClientAuthenticator(provider)

    async def meta(request: Request) -> Response:
        return JSONResponse(metadata(request))

    return [
        Route("/.well-known/oauth-authorization-server",
              endpoint=_api(meta, ["GET", "OPTIONS"], cors=True), methods=["GET", "OPTIONS"]),
        Route("/oauth/authorize", endpoint=_api(
            AuthorizationHandler(provider).handle, ["GET", "POST"], cors=False),
              methods=["GET", "POST"]),
        Route("/oauth/login", endpoint=_api(login_endpoint(provider), ["GET", "POST"], cors=False),
              methods=["GET", "POST"]),
        Route("/oauth/token", endpoint=_api(
            TokenHandler(provider, clients).handle, ["POST", "OPTIONS"], cors=True),
              methods=["POST", "OPTIONS"]),
        Route("/oauth/register", endpoint=_api(RegistrationHandler(
            provider, options=ClientRegistrationOptions(enabled=True)).handle,
            ["POST", "OPTIONS"], cors=True), methods=["POST", "OPTIONS"]),
        Route("/oauth/revoke", endpoint=_api(
            revoke_endpoint(provider, clients), ["POST", "OPTIONS"], cors=True),
              methods=["POST", "OPTIONS"]),
    ]


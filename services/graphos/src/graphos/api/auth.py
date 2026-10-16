"""Sign-in: ``/v1/auth/*`` and ``/v1/me/password``.

A user signs in with a name and password and receives a short-lived access
token; a person also gets a refresh token for their browser session, which
rotates on each use.  The web UI asks for it in the ``graphos_session``
cookie (``session_cookie``): HttpOnly, so page scripts never see it, and it
survives a reload.  A service account has no session: its client signs in
again before the token expires.  See :mod:`graphos.identity`.

With single sign-on configured, ``/v1/auth/oidc/start`` and ``/callback``
run the Authorization Code flow with PKCE for the UI, graphos holding the
verifier and exchanging the code (see :mod:`graphos.oidc`); the session that
results renews like any other.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from urllib.parse import urlsplit

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response, status

from graphos import identity, oidc
from graphos.auth import ReadPrincipal
from graphos.config import Settings
from graphos.contracts.auth import (
    AuthConfig,
    LoginRequest,
    LogoutRequest,
    LogoutResponse,
    OidcCallback,
    OidcConfig,
    OidcSignedIn,
    OidcStart,
    OidcStartResponse,
    PasswordChange,
    RefreshRequest,
    TokenResponse,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# The browser session: the refresh token, where page scripts cannot read it.
SESSION_COOKIE = "graphos_session"

# Error responses the sign-in routes share (``responses=``).
_AUTH_OFF_404: dict[int | str, dict[str, Any]] = {
    404: {"description": "Authentication is disabled on this server (`GRAPHOS_AUTH_DISABLED`)."},
}
_SSO_404: dict[int | str, dict[str, Any]] = {
    404: {"description": (
        "Single sign-on is not configured, or authentication is disabled on this server."
    )},
}
_THROTTLED_429: dict[int | str, dict[str, Any]] = {
    429: {"description": (
        "Too many sign-in attempts from this address; `Retry-After` says when to try again."
    )},
}


@dataclass(frozen=True)
class AuthDeps:
    pool: asyncpg.Pool
    store: identity.AccessStore
    signer: identity.Signer
    throttle: identity.Throttle
    settings: Settings
    provider: oidc.Provider | None


def deps(request: Request) -> AuthDeps:
    store = getattr(request.app.state, "access_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="sign-in is not available: authentication is disabled on this server",
        )
    return AuthDeps(
        pool=request.app.state.pool,
        store=store,
        signer=request.app.state.signer,
        throttle=request.app.state.login_throttle,
        settings=request.app.state.settings,
        provider=getattr(request.app.state, "oidc", None),
    )


def _refused(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc),
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _tokens(d: AuthDeps, user: identity.User, *, session: bool) -> TokenResponse:
    access, ttl = d.signer.issue(user)
    refresh = None
    if session and user.kind == "person":
        refresh = await identity.open_session(
            d.pool, user.name, idle_s=d.settings.session_idle_s, max_s=d.settings.session_max_s,
        )
    return TokenResponse(
        access_token=access, expires_in=ttl, refresh_token=refresh, session=refresh is not None,
        must_change=user.must_change,
    )


Tokens = TypeVar("Tokens", bound=TokenResponse)


def _in_cookie(response: Response, d: AuthDeps, tokens: Tokens) -> Tokens:
    """Moves the refresh token into the session cookie (a browser asked with
    ``session_cookie``): HttpOnly, so the page never holds it."""
    if tokens.refresh_token:
        response.set_cookie(
            SESSION_COOKIE, tokens.refresh_token, max_age=d.settings.session_max_s, path="/",
            secure=d.settings.auth_cookie_secure, httponly=True, samesite="strict",
        )
    return tokens.model_copy(update={"refresh_token": None})


def _clear_cookie(response: Response, d: AuthDeps) -> None:
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=d.settings.auth_cookie_secure, httponly=True,
        samesite="strict",
    )


@router.post(
    "/auth/login",
    response_model=TokenResponse,
    summary="Sign in with a user name and password",
    description=(
        "Returns an access token (`Authorization: Bearer …`, valid `expires_in` seconds) and, "
        "for a person, a refresh token (in the `graphos_session` cookie instead with "
        "`session_cookie`). Failures answer 401 with one message whatever the cause; repeated "
        "failures lock the account for a while, and too many attempts from one address "
        "answer 429."
    ),
    responses={
        401: {"description": (
            "The name or password is wrong, or the account is locked or disabled: one message "
            "whatever the cause."
        )},
        **_AUTH_OFF_404,
        **_THROTTLED_429,
    },
)
async def login(body: LoginRequest, request: Request, response: Response) -> TokenResponse:
    d = deps(request)
    client = request.client.host if request.client else "unknown"
    if not d.throttle.admit(client):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many sign-in attempts; wait a minute",
            headers={"Retry-After": "60"},
        )
    name = body.user.strip().lower()
    if name == identity.ADMIN_USER and not d.settings.local_admin:
        log.warning("sign-in refused: the bootstrap admin is disabled (GRAPHOS_LOCAL_ADMIN=false)")
        raise _refused(identity.AuthError(identity.REFUSED_MESSAGE))
    try:
        user = await d.store.check_password(
            name, body.password,
            max_failures=d.settings.login_max_failures, lockout_s=d.settings.login_lockout_s,
        )
    except identity.AuthError as exc:
        raise _refused(exc) from None
    log.info("signed in", extra={"user": user.name, "kind": user.kind})
    tokens = await _tokens(d, user, session=True)
    return _in_cookie(response, d, tokens) if body.session_cookie else tokens


@router.post(
    "/auth/refresh",
    response_model=TokenResponse,
    summary="Renew a person's access token",
    description=(
        "Exchanges a refresh token (from the body, or the `graphos_session` cookie) for a new "
        "access token and a new refresh token; the old one stops working. A refresh token used "
        "again more than a few seconds after it was rotated revokes the whole session; within "
        "them (another browser tab renewing at the same moment) it gives an access token only."
    ),
    responses={
        401: {"description": (
            "No refresh token, or one that is invalid, expired or revoked; or its user is "
            "disabled or gone."
        )},
        **_AUTH_OFF_404,
    },
)
async def refresh(body: RefreshRequest, request: Request, response: Response) -> TokenResponse:
    d = deps(request)
    token = body.refresh_token or request.cookies.get(SESSION_COOKIE)
    if not token:
        raise _refused(identity.SessionError("no session"))
    try:
        renewed = await identity.rotate_session(d.pool, token, idle_s=d.settings.session_idle_s)
    except identity.SessionError as exc:
        raise _refused(exc) from None
    if renewed.identity is not None:
        # Signed in at the identity provider: issued again from its claims.
        access, ttl = d.signer.issue_for(renewed.identity)
        tokens = TokenResponse(
            access_token=access, expires_in=ttl, refresh_token=renewed.refresh_token, session=True,
        )
    else:
        user = d.store.user(renewed.user)
        if user is None or user.disabled:
            raise _refused(identity.SessionError("session ended"))
        access, ttl = d.signer.issue(user)
        tokens = TokenResponse(
            access_token=access, expires_in=ttl, refresh_token=renewed.refresh_token, session=True,
            must_change=user.must_change,
        )
    from_cookie = body.refresh_token is None
    return _in_cookie(response, d, tokens) if body.session_cookie or from_cookie else tokens


@router.post(
    "/auth/logout",
    response_model=LogoutResponse,
    summary="End a person's session",
    description=(
        "Revokes the session of this refresh token (or of the `graphos_session` cookie, which "
        "is cleared); the access token expires on its own. For a session signed in at the "
        "identity provider the answer carries `end_session_url`: opening it ends the session "
        "there too."
    ),
    responses=_AUTH_OFF_404,
)
async def logout(body: LogoutRequest, request: Request, response: Response) -> LogoutResponse:
    d = deps(request)
    token = body.refresh_token or request.cookies.get(SESSION_COOKIE)
    _clear_cookie(response, d)
    if not token:
        return LogoutResponse()
    ended = await identity.close_session(d.pool, token)
    if ended is None or ended.kind != "oidc" or d.provider is None:
        return LogoutResponse()
    try:
        url = await d.provider.end_session_url(body.post_logout_redirect_uri, ended.id_token)
    except oidc.OidcError:
        url = None
    return LogoutResponse(end_session_url=url)


# ── Single sign-on ───────────────────────────────────────────────────────


@router.get(
    "/auth/config",
    response_model=AuthConfig,
    summary="How to sign in",
    description="Graphos users always; the identity provider when single sign-on is configured.",
)
async def auth_config(request: Request) -> AuthConfig:
    settings: Settings = request.app.state.settings
    provider: oidc.Provider | None = getattr(request.app.state, "oidc", None)
    if provider is None:
        return AuthConfig()
    return AuthConfig(oidc=OidcConfig(
        issuer=provider.issuer, client_id=provider.client_id, scopes=provider.scopes,
        display_name=settings.oidc_display_name,
    ))


def _provider(d: AuthDeps) -> oidc.Provider:
    if d.provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="single sign-on is not configured",
        )
    return d.provider


def _throttle(d: AuthDeps, request: Request) -> None:
    if not d.throttle.admit(request.client.host if request.client else "unknown"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many sign-in attempts; wait a minute", headers={"Retry-After": "60"},
        )


def _unavailable(exc: oidc.OidcError) -> HTTPException:
    log.warning("identity provider unavailable: %s", exc)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="the identity provider could not be reached; try again or sign in with a password",
    )


@router.post(
    "/auth/oidc/start",
    response_model=OidcStartResponse,
    summary="Start signing in at the identity provider",
    description=(
        "Answers the provider's authorization URL for the browser to open. graphos keeps the "
        "PKCE verifier and the nonce for 5 minutes; the browser stores nothing. `silent` asks "
        "the provider not to prompt (single sign-on on page load)."
    ),
    responses={
        **_SSO_404,
        422: {"description": (
            "The request is not valid, or `redirect_uri` is not one graphos accepts "
            "(`GRAPHOS_OIDC_REDIRECT_URIS`)."
        )},
        **_THROTTLED_429,
        502: {"description": "The identity provider could not be reached."},
    },
)
async def oidc_start(body: OidcStart, request: Request) -> OidcStartResponse:
    d = deps(request)
    provider = _provider(d)
    _throttle(d, request)
    allowed = d.settings.oidc_redirect_uris
    parts = urlsplit(body.redirect_uri)
    if (allowed and body.redirect_uri not in allowed) or parts.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="redirect_uri is not one graphos accepts (GRAPHOS_OIDC_REDIRECT_URIS)",
        )
    # Only a page of this UI: no other host, no scheme-relative URL.
    return_to = body.return_to if body.return_to.startswith("/") and \
        not body.return_to.startswith("//") else "/"
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier, challenge = oidc.pkce_pair()
    await d.pool.execute(
        "DELETE FROM graphos_meta.oidc_pending WHERE expires_at < now();",
    )
    await d.pool.execute(
        "INSERT INTO graphos_meta.oidc_pending "
        "(state, nonce, code_verifier, redirect_uri, return_to, expires_at) "
        "VALUES ($1, $2, $3, $4, $5, $6);",
        state, nonce, verifier, body.redirect_uri, return_to,
        datetime.now(UTC) + timedelta(seconds=oidc.PENDING_S),
    )
    try:
        url = await provider.authorization_url(
            state=state, nonce=nonce, challenge=challenge, redirect_uri=body.redirect_uri,
            silent=body.silent,
        )
    except oidc.OidcError as exc:
        raise _unavailable(exc) from None
    return OidcStartResponse(authorization_url=url)


@router.post(
    "/auth/oidc/callback",
    response_model=OidcSignedIn,
    summary="Finish signing in at the identity provider",
    description=(
        "The UI posts what the provider sent back (`state`, and `code` or `error`). graphos "
        "exchanges the code, validates the provider's tokens and answers its own access and "
        "refresh tokens, and the page to return to. An `error` (`login_required` after a "
        "silent attempt) answers 401."
    ),
    responses={
        401: {"description": (
            "The sign-in expired or was already used, the provider did not sign the user in "
            "(`error`), or its answer was not valid."
        )},
        **_SSO_404,
        **_THROTTLED_429,
    },
)
async def oidc_callback(body: OidcCallback, request: Request, response: Response) -> OidcSignedIn:
    d = deps(request)
    provider = _provider(d)
    _throttle(d, request)
    pending = await d.pool.fetchrow(
        "DELETE FROM graphos_meta.oidc_pending WHERE state = $1 "
        "RETURNING nonce, code_verifier, redirect_uri, return_to, expires_at > now() AS live;",
        body.state,
    )
    if pending is None or not pending["live"]:
        raise _refused(identity.AuthError("this sign-in expired or was already used; start again"))
    if body.error or not body.code:
        raise _refused(identity.AuthError(
            f"the identity provider did not sign you in ({body.error or 'no code'})",
        ))
    try:
        who, id_token = await provider.sign_in(
            code=body.code, verifier=pending["code_verifier"],
            redirect_uri=pending["redirect_uri"], nonce=pending["nonce"],
        )
    except oidc.OidcError as exc:
        log.warning("single sign-on refused: %s", exc)
        raise _refused(identity.AuthError("the identity provider's answer was not valid")) from None
    access, ttl = d.signer.issue_for(who)
    refresh = await identity.open_session(
        d.pool, identity.OIDC_SUB_PREFIX + who.subject,
        idle_s=d.settings.session_idle_s, max_s=d.settings.session_max_s, identity=who,
        id_token=id_token,
    )
    log.info("signed in through the identity provider", extra={"user": who.consumer})
    signed = OidcSignedIn(
        access_token=access, expires_in=ttl, refresh_token=refresh, session=True,
        return_to=pending["return_to"],
    )
    return _in_cookie(response, d, signed) if body.session_cookie else signed


@router.post(
    "/me/password",
    response_model=TokenResponse,
    summary="Change your password",
    description=(
        "Needs the current password. Every other session and token of yours stops working; "
        "the answer carries new ones. A user whose password must change can call only this, "
        "`GET /v1/me` and `/v1/auth/logout`."
    ),
    responses={
        400: {"description": "The caller signed in at the identity provider: no graphos password."},
        403: {"description": (
            "The current password is wrong; or the token's grants do not allow this operation."
        )},
        **_AUTH_OFF_404,
        422: {"description": (
            "The request is not valid, or the new password does not meet the password policy."
        )},
    },
)
async def change_password(
    body: PasswordChange, request: Request, response: Response, principal: ReadPrincipal,
) -> TokenResponse:
    d = deps(request)
    if principal.subject is None or principal.via != "password":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no graphos password to change: you signed in at the identity provider",
        )
    name = principal.consumer
    try:
        await d.store.check_password(
            name, body.current,
            max_failures=d.settings.login_max_failures, lockout_s=d.settings.login_lockout_s,
        )
    except identity.AuthError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="the current password is wrong",
        ) from None
    try:
        identity.check_policy(
            name, body.new, min_length=d.settings.password_min_length, current=body.current,
        )
    except identity.PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc),
        ) from None
    await d.store.set_password(name, body.new, must_change=False)
    log.info("password changed", extra={"user": name})
    user = d.store.user(name)
    assert user is not None
    tokens = await _tokens(d, user, session=True)
    return _in_cookie(response, d, tokens) if body.session_cookie else tokens

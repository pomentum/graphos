"""Contracts for sign-in, users and grants (``/v1/auth``, ``/v1/users``, ``/v1/grants``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

UserKind = Literal["person", "service"]
GrantLevel = Literal["read", "write", "maintain", "owner", "create_tenants", "admin"]


class LoginRequest(BaseModel):
    """A sign-in with a user name and password (`POST /v1/auth/login`)."""

    user: str = Field(
        ..., min_length=1, max_length=63,
        description="The user name; surrounding spaces are dropped and case is ignored.",
    )
    password: str = Field(
        ..., min_length=1, max_length=1024,
        description="The user's password, or a service account's secret.",
    )
    session_cookie: bool = Field(
        default=False,
        description=(
            "For a browser: keep the refresh token in the `graphos_session` cookie (HttpOnly, "
            "SameSite=Strict) instead of the answer, so page scripts never see it."
        ),
    )


class TokenResponse(BaseModel):
    """An access token, and for a person the session that renews it."""

    access_token: str = Field(
        ..., description="The signed access token, sent as `Authorization: Bearer <token>`.",
    )
    token_type: Literal["Bearer"] = Field("Bearer", description="Always `Bearer`.")
    expires_in: int = Field(..., description="Seconds the access token is valid.")
    refresh_token: str | None = Field(
        None,
        description=(
            "People only: renews the access token (`POST /v1/auth/refresh`) and rotates. "
            "Keep it in memory; a service account signs in again instead. Absent when the "
            "request asked for `session_cookie`: the cookie holds it."
        ),
    )
    session: bool = Field(
        default=False,
        description="A session can renew this token: `refresh_token`, or the session cookie.",
    )
    must_change: bool = Field(
        default=False, description="The password must be changed before anything else.",
    )


class RefreshRequest(BaseModel):
    """Renews a person's access token (`POST /v1/auth/refresh`)."""

    refresh_token: str | None = Field(
        default=None, min_length=1, max_length=256,
        description="The refresh token; when absent, the one in the session cookie.",
    )
    session_cookie: bool = Field(
        default=False,
        description=(
            "For a browser: keep the refresh token in the `graphos_session` cookie (HttpOnly, "
            "SameSite=Strict) instead of the answer, so page scripts never see it."
        ),
    )


class LogoutRequest(RefreshRequest):
    """Ends a person's session (`POST /v1/auth/logout`); the access token expires on its own."""

    post_logout_redirect_uri: str | None = Field(
        None, max_length=2048,
        description="Where the identity provider sends the browser after signing out there.",
    )


class LogoutResponse(BaseModel):
    """The answer to a sign-out."""

    end_session_url: str | None = Field(
        default=None,
        description=(
            "For a session signed in at the identity provider: open it to end the session "
            "there too (single sign-out)."
        ),
    )


class OidcConfig(BaseModel):
    """The identity provider single sign-on uses (`GRAPHOS_OIDC_*`)."""

    issuer: str = Field(..., description="The provider's issuer URL (`GRAPHOS_OIDC_ISSUER`).")
    client_id: str = Field(
        ..., description="graphos's client id at the provider (`GRAPHOS_OIDC_CLIENT_ID`).",
    )
    scopes: str = Field(
        ...,
        description=(
            "The scopes requested, separated by spaces (`GRAPHOS_OIDC_SCOPES`, default "
            "`openid profile email`)."
        ),
    )
    display_name: str = Field(
        ...,
        description=(
            "The provider's name for the sign-in button (`GRAPHOS_OIDC_DISPLAY_NAME`, "
            "default `SSO`)."
        ),
    )


class AuthConfig(BaseModel):
    """How to sign in, for the UI: graphos users always; SSO when configured."""

    password: bool = Field(
        True, description="Signing in with a graphos user name and password; always true.",
    )
    oidc: OidcConfig | None = Field(
        None, description="The identity provider; null when single sign-on is not configured.",
    )


class OidcStart(BaseModel):
    """Starts signing in at the identity provider (`POST /v1/auth/oidc/start`)."""

    redirect_uri: str = Field(
        ..., max_length=2048,
        description=(
            "The UI's callback address: `http` or `https`, and one of "
            "`GRAPHOS_OIDC_REDIRECT_URIS` when that is set; else 422."
        ),
    )
    return_to: str = Field(
        "/", max_length=2048,
        description=(
            "The UI page to come back to: a path starting with `/`; anything else (another "
            "host, a URL starting with `//`) becomes `/`."
        ),
    )
    silent: bool = Field(
        False,
        description="`prompt=none`: come back at once, signed in or with `login_required`.",
    )


class OidcStartResponse(BaseModel):
    """Where the browser goes to sign in at the identity provider."""

    authorization_url: str = Field(
        ...,
        description=(
            "The provider's authorization URL, carrying the state, nonce and PKCE challenge; "
            "the sign-in must finish within 5 minutes."
        ),
    )


class OidcCallback(BaseModel):
    """What the identity provider sent back to the UI (`POST /v1/auth/oidc/callback`)."""

    state: str = Field(
        ..., min_length=1, max_length=256,
        description=(
            "The `state` the provider sent back; usable once, within 5 minutes of the start, "
            "else 401."
        ),
    )
    code: str | None = Field(
        None, max_length=4096,
        description="The authorization code; absent when the provider answered an `error`.",
    )
    error: str | None = Field(
        None, max_length=256,
        description=(
            "The provider's error (`login_required` after a silent attempt). With an error, or "
            "without a `code`, the answer is 401."
        ),
    )
    session_cookie: bool = Field(
        default=False,
        description=(
            "For a browser: keep the refresh token in the `graphos_session` cookie (HttpOnly, "
            "SameSite=Strict) instead of the answer, so page scripts never see it."
        ),
    )


class OidcSignedIn(TokenResponse):
    """graphos's own tokens after signing in at the identity provider."""

    return_to: str = Field(
        "/", description="The UI page to go back to, as given to `POST /v1/auth/oidc/start`.",
    )


class PasswordChange(BaseModel):
    """Changes the caller's own password (`POST /v1/me/password`)."""

    current: str = Field(
        ..., min_length=1, max_length=1024,
        description="The current password; a wrong one answers 403.",
    )
    new: str = Field(
        ..., min_length=1, max_length=1024,
        description=(
            "The new password. It must meet the password policy (at least "
            "`GRAPHOS_PASSWORD_MIN_LENGTH` characters, not the current one), else 422."
        ),
    )
    session_cookie: bool = Field(
        default=False,
        description=(
            "For a browser: keep the refresh token in the `graphos_session` cookie (HttpOnly, "
            "SameSite=Strict) instead of the answer, so page scripts never see it."
        ),
    )


class UserInfo(BaseModel):
    """A user: a person or a service account. Never its password."""

    name: str = Field(..., description="The user name.")
    kind: UserKind = Field(
        ...,
        description=(
            "`person`: signs in with a password and gets a refresh token; `service`: a service "
            "account, whose client signs in again with its secret instead."
        ),
    )
    disabled: bool = Field(
        ..., description="A disabled user cannot sign in and its tokens stop working.",
    )
    must_change: bool = Field(
        ...,
        description=(
            "The password must be changed before anything else: until then the user may call "
            "only `GET /v1/me`, `POST /v1/me/password` and logout."
        ),
    )
    created_at: datetime = Field(..., description="When the user was created.")
    created_by: str | None = Field(
        None,
        description=(
            "Who created it: an admin's name, `bootstrap` (the first start) or "
            "`reset-admin-password`."
        ),
    )
    last_login_at: datetime | None = Field(
        None, description="The last successful password sign-in; null when it never signed in.",
    )


class UserCreate(BaseModel):
    """A new user (`POST /v1/users`, admin only)."""

    name: str = Field(
        ..., description="Lower case letters, digits, `_`, `.`, `-`; starts with a letter.",
    )
    kind: UserKind = Field(
        "person",
        description=(
            "`person` (changes the password at the first sign-in) or `service` (keeps it as "
            "its secret)."
        ),
    )


class UserCreated(BaseModel):
    """A user, and its new password shown this once."""

    user: UserInfo = Field(..., description="The user.")
    password: str = Field(
        ...,
        description=(
            "Shown once and never stored: a person changes it at the first sign-in; a "
            "service account keeps it as its secret."
        ),
    )


class UserUpdate(BaseModel):
    """Disables or enables a user (`PATCH /v1/users/{name}`, admin only)."""

    disabled: bool = Field(
        ...,
        description=(
            "True disables the user and ends its sessions; false enables it. Either way its "
            "tokens stop working."
        ),
    )


class UserList(BaseModel):
    """Every user."""

    users: list[UserInfo] = Field(..., description="The users, sorted by name.")


class GrantSpec(BaseModel):
    """A grant: a subject's level on a tenant or pattern, or a global right."""

    subject: str = Field(
        ...,
        description=(
            "`user:<name>`, or with single sign-on `oidc-sub:<sub>`, `oidc-group:<group>`, "
            "`oidc-role:<role>`, `oidc-client:<client id>`."
        ),
        max_length=300,
    )
    tenant: str = Field(
        ...,
        description="A tenant name, or a pattern with `*` (`sol_*`, `*`); `*` for `admin`.",
    )
    level: GrantLevel = Field(
        ...,
        description=(
            "On a tenant: `read` < `write` < `maintain` < `owner`, each including the ones "
            "before it. Global rights: `create_tenants` (create tenants matching the pattern, "
            "which the creator then owns) and `admin` (everything; granted on `*` only)."
        ),
    )


class GrantInfo(GrantSpec):
    """A stored grant."""

    granted_by: str | None = Field(
        None,
        description=(
            "Who gave it: a user's name, `creator` (a tenant creator's `owner` grant), "
            "`bootstrap` or `reset-admin-password`."
        ),
    )
    granted_at: datetime = Field(..., description="When it was given.")


class GrantList(BaseModel):
    """Grants."""

    grants: list[GrantInfo] = Field(
        ...,
        description=(
            "The grants the caller may see (an admin: all; others: their own and those on the "
            "tenants they own), sorted by subject, tenant and level."
        ),
    )

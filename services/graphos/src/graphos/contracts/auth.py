"""Contracts for sign-in, users and grants (``/v1/auth``, ``/v1/users``, ``/v1/grants``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

UserKind = Literal["person", "service"]
GrantLevel = Literal["read", "write", "maintain", "owner", "create_tenants", "admin"]


class LoginRequest(BaseModel):
    user: str = Field(..., min_length=1, max_length=63)
    password: str = Field(..., min_length=1, max_length=1024)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int = Field(..., description="Seconds the access token is valid.")
    refresh_token: str | None = Field(
        None,
        description=(
            "People only: renews the access token (`POST /v1/auth/refresh`) and rotates. "
            "Keep it in memory; a service account signs in again instead."
        ),
    )
    must_change: bool = Field(
        default=False, description="The password must be changed before anything else.",
    )


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1, max_length=256)


class LogoutRequest(RefreshRequest):
    post_logout_redirect_uri: str | None = Field(
        None, max_length=2048,
        description="Where the identity provider sends the browser after signing out there.",
    )


class LogoutResponse(BaseModel):
    end_session_url: str | None = Field(
        default=None,
        description=(
            "For a session signed in at the identity provider: open it to end the session "
            "there too (single sign-out)."
        ),
    )


class OidcConfig(BaseModel):
    issuer: str
    client_id: str
    scopes: str
    display_name: str


class AuthConfig(BaseModel):
    """How to sign in, for the UI: graphos users always; SSO when configured."""

    password: bool = True
    oidc: OidcConfig | None = None


class OidcStart(BaseModel):
    redirect_uri: str = Field(..., max_length=2048, description="The UI's callback address.")
    return_to: str = Field("/", max_length=2048, description="The UI page to come back to.")
    silent: bool = Field(
        False,
        description="`prompt=none`: come back at once, signed in or with `login_required`.",
    )


class OidcStartResponse(BaseModel):
    authorization_url: str


class OidcCallback(BaseModel):
    state: str = Field(..., min_length=1, max_length=256)
    code: str | None = Field(None, max_length=4096)
    error: str | None = Field(None, max_length=256)


class OidcSignedIn(TokenResponse):
    return_to: str = "/"


class PasswordChange(BaseModel):
    current: str = Field(..., min_length=1, max_length=1024)
    new: str = Field(..., min_length=1, max_length=1024)


class UserInfo(BaseModel):
    name: str
    kind: UserKind
    disabled: bool
    must_change: bool
    created_at: datetime
    created_by: str | None = None
    last_login_at: datetime | None = None


class UserCreate(BaseModel):
    name: str = Field(
        ..., description="Lower case letters, digits, `_`, `.`, `-`; starts with a letter.",
    )
    kind: UserKind = "person"


class UserCreated(BaseModel):
    user: UserInfo
    password: str = Field(
        ...,
        description=(
            "Shown once and never stored: a person changes it at the first sign-in; a "
            "service account keeps it as its secret."
        ),
    )


class UserUpdate(BaseModel):
    disabled: bool


class UserList(BaseModel):
    users: list[UserInfo]


class GrantSpec(BaseModel):
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
    level: GrantLevel


class GrantInfo(GrantSpec):
    granted_by: str | None = None
    granted_at: datetime


class GrantList(BaseModel):
    grants: list[GrantInfo]

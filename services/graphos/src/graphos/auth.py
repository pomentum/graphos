"""Bearer authentication and per-route access.

A request carries ``Authorization: Bearer <access token>``: a short-lived
token graphos signed, for one of its users (``POST /v1/auth/login``, see
:mod:`graphos.identity`) or for someone who signed in at the identity
provider (``/v1/auth/oidc/*``, see :mod:`graphos.oidc`), or an access token
of that provider itself, sent by a program.  The token says who; what they
may do comes from their grants, read from memory on every request, so a
revoked grant or a disabled user applies at once.

Every authenticated route declares the access it needs by taking its
principal as one of :data:`ReadPrincipal`, :data:`WritePrincipal`,
:data:`MaintainPrincipal`, :data:`OwnerPrincipal` or :data:`AdminPrincipal`:
a FastAPI ``Security`` dependency whose scope the OpenAPI schema shows on
each operation.  ``tests/test_access.py`` fails on an operation that
declares none and is not one of the few public ones.  On a route with a
``{tenant}`` path parameter the dependency also holds the caller to that
level on that tenant, so a tenant route cannot forget it; other routes
check the tenant they act on themselves (:func:`require_level`).

A user whose password must change (the bootstrap admin, a reset) may call
only :data:`MUST_CHANGE_ALLOWED` until it is changed.

The dependency records the access in the request scope, where the audit
log reads it, and the consumer in :data:`graphos.changes.ACTOR` for the
change records of the request's writes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Protocol

from fastapi import Depends, HTTPException, Path, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, SecurityScopes

from graphos.changes import ACTOR, Actor
from graphos.identity import (
    ANONYMOUS_ADMIN,
    OIDC_SUB_PREFIX,
    AccessStore,
    Level,
    Principal,
    Signer,
    TokenError,
)
from graphos.oidc import Identity, OidcError, Provider

log = logging.getLogger(__name__)


class Access(StrEnum):
    """What a route requires: a level on its tenant, or admin."""

    READ = "read"
    WRITE = "write"
    MAINTAIN = "maintain"
    OWNER = "owner"
    ADMIN = "admin"

    @property
    def level(self) -> Level | None:
        """The tenant level it needs; None for ``admin``."""
        return None if self is Access.ADMIN else Level.parse(self.value)


# Request-scope key under which require_principal records a route's access.
ACCESS_SCOPE_KEY = "graphos.access"

# What a user whose password must change may call.
MUST_CHANGE_ALLOWED = frozenset({"/v1/me", "/v1/me/password", "/v1/auth/logout"})


class Authenticator(Protocol):
    async def authenticate(self, bearer: str) -> Principal | None: ...


class TokenAuthenticator:
    """graphos's own access tokens, checked against the live user record, and
    (with single sign-on configured) the identity provider's access tokens."""

    def __init__(
        self, signer: Signer, store: AccessStore, provider: Provider | None = None,
    ) -> None:
        self.signer = signer
        self.store = store
        self.provider = provider

    async def authenticate(self, bearer: str) -> Principal | None:
        try:
            claims = self.signer.verify(bearer)
        except TokenError:
            return await self._provider_token(bearer)
        subject = str(claims["sub"])
        if subject.startswith(OIDC_SUB_PREFIX):
            identity = claims.get("idn")
            if not isinstance(identity, dict):
                return None
            return self.store.principal_for(Identity.from_claims(identity))
        user = self.store.user(subject)
        # A password change, reset or disable bumps the version: older tokens stop.
        if user is None or user.disabled or claims.get("ver") != user.token_version:
            return None
        return self.store.principal(user.name)

    async def _provider_token(self, bearer: str) -> Principal | None:
        """A program's token from the identity provider, for graphos's audience."""
        if self.provider is None:
            return None
        try:
            claims = await self.provider.validate(bearer, audience=self.provider.audience)
        except OidcError:
            return None
        person = any(claims.get(c) for c in ("preferred_username", "email", "name"))
        return self.store.principal_for(
            self.provider.identity(claims), kind="person" if person else "service",
        )


class StaticAuthenticator:
    """Fixed bearer strings for fixed principals: tests only, never configured."""

    def __init__(self, principals: Mapping[str, Principal]) -> None:
        self._principals = dict(principals)

    async def authenticate(self, bearer: str) -> Principal | None:
        return self._principals.get(bearer)


_bearer = HTTPBearer(
    auto_error=False,
    description=(
        "An access token from `POST /v1/auth/login`.  Each operation's scope is the "
        "access it needs: `read`, `write`, `maintain` or `owner` on its tenant, or `admin`."
    ),
)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def authenticate_request(request: Request, bearer: str | None) -> Principal:
    """The principal of a request's bearer token.

    Raises:
        HTTPException: 401 without a valid token.
    """
    authenticator: Authenticator | None = getattr(request.app.state, "authenticator", None)
    if authenticator is None:
        settings = getattr(request.app.state, "settings", None)
        if settings is None or not settings.auth_disabled:
            # Nothing to check tokens with and auth not explicitly off: fail closed.
            raise _unauthorized("authentication is not configured on this server")
        return ANONYMOUS_ADMIN
    if not bearer:
        raise _unauthorized("missing or malformed Authorization header")
    principal = await authenticator.authenticate(bearer.strip())
    if principal is None:
        raise _unauthorized("invalid or expired token")
    return principal


async def require_principal(
    request: Request,
    security_scopes: SecurityScopes,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """FastAPI dependency: the caller's principal, held to the route's access.

    Use it through the ``*Principal`` aliases, which set its scope.  Raises
    401 without a valid token, and 403 when the password must change first,
    when the route needs admin, or when the path's ``{tenant}`` needs a
    level the caller lacks.  A route that declares no scope is held to
    ``write``.

    The access is recorded in ``request.scope[ACCESS_SCOPE_KEY]`` before the
    token is checked, and the principal in ``request.scope["principal"]``,
    so the audit middleware can tell a write (even a refused one) and who
    made it.  This dependency is async so that it runs in the request's own
    task, where the endpoint sees :data:`ACTOR`.
    """
    scopes = security_scopes.scopes
    needed = Access(scopes[0]) if len(scopes) == 1 else Access.WRITE
    request.scope[ACCESS_SCOPE_KEY] = needed
    principal = await authenticate_request(
        request, credentials.credentials if credentials else None,
    )
    request.scope["principal"] = principal
    ACTOR.set(Actor(consumer=principal.consumer))
    if principal.must_change and request.url.path not in MUST_CHANGE_ALLOWED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="change your password first (POST /v1/me/password)",
        )
    if needed is Access.ADMIN:
        require_admin(principal)
    tenant = request.path_params.get("tenant")
    if tenant is not None:
        require_level(principal, tenant, needed.level or Level.READ)
        # Registered through another replica since this one last looked?
        # Load it now rather than answer 404 (``graphos.sync``).
        registry = getattr(request.app.state, "registry_sync", None)
        if registry is not None:
            await registry.ensure(tenant)
    return principal


ReadPrincipal = Annotated[Principal, Security(require_principal, scopes=[Access.READ.value])]
WritePrincipal = Annotated[Principal, Security(require_principal, scopes=[Access.WRITE.value])]
MaintainPrincipal = Annotated[
    Principal, Security(require_principal, scopes=[Access.MAINTAIN.value]),
]
OwnerPrincipal = Annotated[Principal, Security(require_principal, scopes=[Access.OWNER.value])]
AdminPrincipal = Annotated[Principal, Security(require_principal, scopes=[Access.ADMIN.value])]

Tenant = Annotated[str, Path(description="The tenant's name, as registered.")]


def require_level(principal: Principal, tenant: str, needed: Level) -> None:
    """403 unless ``principal`` holds ``needed`` (or more) on ``tenant``."""
    level = principal.level(tenant)
    if level is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"no access to tenant '{tenant}'",
        )
    if level < needed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"'{needed.label}' access to tenant '{tenant}' is needed "
                f"(you have '{level.label}')"
            ),
        )


def require_admin(principal: Principal) -> None:
    if not principal.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin access required",
        )


def require_tenant(principal: Principal, tenant: str) -> None:
    """Caller may at least read this tenant."""
    require_level(principal, tenant, Level.READ)

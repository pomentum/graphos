"""Bearer-token auth and per-route access.

Tokens are loaded from a JSON file at startup (and on SIGHUP — see app.py).
File shape::

    {
      "tok_abc...": {
        "consumer": "app-reader",
        "tenants":  ["demo"],
        "admin":    false,
        "access":   "read"
      },
      "tok_xyz...": {
        "consumer": "ops",
        "tenants":  ["demo"],
        "admin":    true,
        "access":   "write"
      }
    }

``access`` is ``"read"`` or ``"write"``.  A token without it gets
``"write"`` (what every token could do before the field existed), and a
warning is logged until the file says so explicitly.  It is independent of
``admin``: a read-only admin can list tenants and database statistics but
change nothing.

Every authenticated route declares the access it needs by taking its
principal as :data:`ReadPrincipal` or :data:`WritePrincipal`: a FastAPI
``Security`` dependency whose scope (``read`` / ``write``) the OpenAPI
schema shows on each operation.  ``tests/test_access.py`` fails on an
operation that declares neither and is not one of the few public ones.
The same dependency enforces the tenant scope of any route with a
``{tenant}`` path parameter, so a tenant route cannot forget it, and
records the access in the request scope, where the audit log reads it.

Lookup is O(1). Reload is atomic — the token map is swapped under a lock.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, SecurityScopes

log = logging.getLogger(__name__)


class Access(StrEnum):
    """What a token may do, and what a route requires."""

    READ = "read"
    WRITE = "write"


# Request-scope key under which require_principal records a route's access.
ACCESS_SCOPE_KEY = "graphos.access"


@dataclass(frozen=True, slots=True)
class TokenPrincipal:
    consumer: str
    tenants: frozenset[str]
    admin: bool
    access: Access = Access.WRITE


class TokenStore:
    """In-memory token → principal map with atomic reload."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._tokens: dict[str, TokenPrincipal] = {}
        # Why the last load failed (None after a good load); the app refuses
        # to start while this is set, so a broken file is never silent.
        self.load_error: str | None = None
        if path is not None:
            self.reload()

    def reload(self) -> int:
        """Reload tokens from file. Returns the number of tokens in effect.

        A file that cannot be read or is not a JSON object leaves the tokens
        already loaded in place (a typo before ``SIGHUP`` must not lock every
        consumer out) and records ``load_error``.  Malformed entries are
        skipped; the rest load.
        """
        if self._path is None:
            return 0
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("top level must be a JSON object")
        except (OSError, ValueError) as exc:
            self.load_error = f"{type(exc).__name__}: {exc}"
            log.error(
                "auth tokens file unusable; keeping the tokens already loaded",
                extra={"path": str(self._path), "error": self.load_error},
            )
            with self._lock:
                return len(self._tokens)
        new_map: dict[str, TokenPrincipal] = {}
        implicit_write: list[str] = []
        for token, principal in raw.items():
            try:
                access = principal.get("access")
                new_map[str(token)] = TokenPrincipal(
                    consumer=str(principal["consumer"]),
                    tenants=frozenset(str(t) for t in principal.get("tenants", [])),
                    admin=bool(principal.get("admin", False)),
                    access=Access.WRITE if access is None else _token_access(access),
                )
                if access is None:
                    implicit_write.append(str(principal["consumer"]))
            except (KeyError, TypeError, AttributeError, ValueError):
                # Never log the token itself; skip the bad entry, keep the rest.
                log.error(
                    "auth tokens file: skipping malformed entry",
                    extra={"path": str(self._path)},
                )
        with self._lock:
            self._tokens = new_map
        self.load_error = None
        log.info("auth tokens reloaded", extra={"count": len(new_map)})
        if implicit_write:
            log.warning(
                'auth tokens without "access" are given write access; set '
                '"access": "read" or "write" on each',
                extra={"path": str(self._path), "consumers": sorted(implicit_write)},
            )
        return len(new_map)

    def lookup(self, token: str) -> TokenPrincipal | None:
        with self._lock:
            return self._tokens.get(token)


_ANONYMOUS_ADMIN = TokenPrincipal(consumer="anonymous", tenants=frozenset(), admin=True)

_bearer = HTTPBearer(
    auto_error=False,
    description=(
        "A token from GRAPHOS_AUTH_TOKENS_FILE.  Each operation's scope is the "
        "access its token needs: `read` or `write`."
    ),
)


def _authenticate(
    request: Request, credentials: HTTPAuthorizationCredentials | None,
) -> TokenPrincipal:
    store: TokenStore | None = getattr(request.app.state, "token_store", None)
    if store is None:
        settings = getattr(request.app.state, "settings", None)
        if settings is None or not settings.auth_disabled:
            # No token store and auth not explicitly disabled: fail closed.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication is not configured on this server",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return _ANONYMOUS_ADMIN
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    principal = store.lookup(credentials.credentials.strip())
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def require_principal(
    request: Request,
    security_scopes: SecurityScopes,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> TokenPrincipal:
    """FastAPI dependency: the caller's principal, held to the route's access.

    Use it through :data:`ReadPrincipal` / :data:`WritePrincipal`, which
    set its scope.  Raises 401 without a valid token, and 403 when the
    token has no access to the path's ``{tenant}`` or the route needs write
    and the token is read-only.  A route that declares no scope is held to
    write.

    The access is recorded in ``request.scope[ACCESS_SCOPE_KEY]`` before the
    token is checked, and the principal in ``request.scope["principal"]``,
    so the audit middleware can tell a write (even a refused one) and who
    made it.
    """
    needed = Access.READ if security_scopes.scopes == [Access.READ.value] else Access.WRITE
    request.scope[ACCESS_SCOPE_KEY] = needed
    principal = _authenticate(request, credentials)
    request.scope["principal"] = principal
    tenant = request.path_params.get("tenant")
    if tenant is not None:
        require_tenant_access(principal, tenant)
    if needed is Access.WRITE and principal.access is not Access.WRITE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this token is read-only",
        )
    return principal


ReadPrincipal = Annotated[TokenPrincipal, Security(require_principal, scopes=[Access.READ.value])]
WritePrincipal = Annotated[TokenPrincipal, Security(require_principal, scopes=[Access.WRITE.value])]


def _token_access(value: object) -> Access:
    if value not in (Access.READ, Access.WRITE):
        raise ValueError(f"access must be 'read' or 'write', not {value!r}")
    return Access(value)


def require_tenant_access(principal: TokenPrincipal, tenant: str) -> None:
    if principal.admin:
        return
    if tenant not in principal.tenants:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"token has no access to tenant '{tenant}'",
        )


def require_admin(principal: TokenPrincipal) -> None:
    if not principal.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin scope required",
        )


def require_tenant(principal: TokenPrincipal, tenant: str) -> None:
    """Caller's token must include this tenant (or be admin-scoped)."""
    require_tenant_access(principal, tenant)


"""Bearer-token auth middleware.

Tokens are loaded from a JSON file at startup (and on SIGHUP — see app.py).
File shape::

    {
      "tok_abc...": {
        "consumer": "app-reader",
        "tenants":  ["demo"],
        "admin":    false
      },
      "tok_xyz...": {
        "consumer": "ops",
        "tenants":  ["demo"],
        "admin":    true
      }
    }

Lookup is O(1). Reload is atomic — the token map is swapped under a lock.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request, status

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TokenPrincipal:
    consumer: str
    tenants: frozenset[str]
    admin: bool


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
        for token, principal in raw.items():
            try:
                new_map[str(token)] = TokenPrincipal(
                    consumer=str(principal["consumer"]),
                    tenants=frozenset(str(t) for t in principal.get("tenants", [])),
                    admin=bool(principal.get("admin", False)),
                )
            except (KeyError, TypeError, AttributeError):
                # Never log the token itself; skip the bad entry, keep the rest.
                log.error(
                    "auth tokens file: skipping malformed entry",
                    extra={"path": str(self._path)},
                )
        with self._lock:
            self._tokens = new_map
        self.load_error = None
        log.info("auth tokens reloaded", extra={"count": len(new_map)})
        return len(new_map)

    def lookup(self, token: str) -> TokenPrincipal | None:
        with self._lock:
            return self._tokens.get(token)


def extract_bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return auth.split(" ", 1)[1].strip()


_ANONYMOUS_ADMIN = TokenPrincipal(consumer="anonymous", tenants=frozenset(), admin=True)


def require_principal(request: Request) -> TokenPrincipal:
    """FastAPI dependency: returns principal or raises 401.

    The principal is also stored in ``request.scope["principal"]`` so the
    audit middleware can record which consumer made a write.
    """
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
        request.scope["principal"] = _ANONYMOUS_ADMIN
        return _ANONYMOUS_ADMIN
    token = extract_bearer(request)
    principal = store.lookup(token)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.scope["principal"] = principal
    return principal


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

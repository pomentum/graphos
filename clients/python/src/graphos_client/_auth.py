"""Signing in to graphos: short-lived access tokens, renewed by the client.

graphos issues an access token (valid ``expires_in`` seconds, 15 minutes by
default) for a user name and password (``POST /v1/auth/login``).  A service
account's password is its secret.  :class:`PasswordAuth` signs in before the
first request, signs in again when 80% of the token's life has passed, and,
should a request still get ``401`` (the token was revoked, the server
restarted with a new key), signs in once more and sends the request again.
That retry is safe for writes: graphos checks the token before doing
anything, so a ``401`` request changed nothing.

The credentials are kept, not just the token, so renewal never needs a
person.  Nothing is written to disk.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator, Awaitable, Callable

import httpx

from graphos_client._errors import GraphosError

USER_ENV = "GRAPHOS_USER"
PASSWORD_ENV = "GRAPHOS_PASSWORD"  # noqa: S105 — the variable's name, not a password


class PasswordAuth(httpx.Auth):
    """A graphos user name and password (or service account and secret).

    Args:
        user: the user or service account name.
        password: its password or secret.
        renew_after: the fraction of a token's lifetime after which it is
            renewed before use.
    """

    def __init__(self, user: str, password: str, *, renew_after: float = 0.8) -> None:
        if not user or not password:
            raise ValueError("PasswordAuth needs a user and a password")
        self.user = user
        self._password = password
        self._renew_after = renew_after
        self._login_url: str | None = None
        self._token: str | None = None
        self._renew_at = 0.0
        self._lock: asyncio.Lock | None = None

    def __repr__(self) -> str:  # never the password or the token
        return f"PasswordAuth(user={self.user!r})"

    def bind(self, base_url: str) -> PasswordAuth:
        """Where to sign in: the client's base URL."""
        self._login_url = f"{base_url.rstrip('/')}/v1/auth/login"
        return self

    def _stale(self) -> bool:
        return self._token is None or time.monotonic() >= self._renew_at

    def _login_request(self, request: httpx.Request) -> httpx.Request:
        url = self._login_url or str(request.url.copy_with(path="/v1/auth/login", query=None))
        return httpx.Request(
            "POST", url, json={"user": self.user, "password": self._password},
            headers={"Accept": "application/json"},
        )

    async def _store(self, response: httpx.Response) -> None:
        await response.aread()
        if response.status_code != httpx.codes.OK:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            self._token = None
            raise GraphosError(response.status_code, f"sign-in failed: {detail}", "POST",
                               str(response.request.url))
        body = response.json()
        self._token = body["access_token"]
        self._renew_at = time.monotonic() + float(body["expires_in"]) * self._renew_after

    async def async_auth_flow(
        self, request: httpx.Request,
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if self._lock is None:
            self._lock = asyncio.Lock()
        if self._stale():
            # One sign-in at a time: concurrent requests wait for it.
            async with self._lock:
                if self._stale():
                    await self._store((yield self._login_request(request)))
        request.headers["Authorization"] = f"Bearer {self._token}"
        response = yield request
        if response.status_code == httpx.codes.UNAUTHORIZED:
            # Revoked, or signed by a key the server no longer has: sign in
            # again once and send the request again.
            self._token = None
            async with self._lock:
                if self._stale():
                    await self._store((yield self._login_request(request)))
            request.headers["Authorization"] = f"Bearer {self._token}"
            yield request

    def sync_auth_flow(self, request: httpx.Request):  # noqa: ANN201, ARG002
        raise RuntimeError("PasswordAuth works with the async client only")


TokenSource = Callable[[], Awaitable[tuple[str, float]]]


class BearerAuth(httpx.Auth):
    """Tokens a program obtains itself, typically from the identity provider
    graphos trusts (client-credentials grant).

    Args:
        source: an async callable answering ``(access token, seconds valid)``;
            called before the first request, again at ``renew_after`` of the
            token's life, and once more on a ``401``.
    """

    def __init__(self, source: TokenSource, *, renew_after: float = 0.8) -> None:
        self._source = source
        self._renew_after = renew_after
        self._token: str | None = None
        self._renew_at = 0.0
        self._lock: asyncio.Lock | None = None

    def __repr__(self) -> str:
        return "BearerAuth()"

    async def _fresh(self, *, force: bool = False) -> str:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if force or self._token is None or time.monotonic() >= self._renew_at:
                token, valid_s = await self._source()
                self._token = token
                self._renew_at = time.monotonic() + float(valid_s) * self._renew_after
            return self._token

    async def async_auth_flow(
        self, request: httpx.Request,
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {await self._fresh()}"
        response = yield request
        if response.status_code == httpx.codes.UNAUTHORIZED:
            request.headers["Authorization"] = f"Bearer {await self._fresh(force=True)}"
            yield request

    def sync_auth_flow(self, request: httpx.Request):  # noqa: ANN201, ARG002
        raise RuntimeError("BearerAuth works with the async client only")


def from_environment() -> PasswordAuth | None:
    """``PasswordAuth`` from ``GRAPHOS_USER`` / ``GRAPHOS_PASSWORD``, if both are set."""
    user, password = os.environ.get(USER_ENV), os.environ.get(PASSWORD_ENV)
    if user and password:
        return PasswordAuth(user, password)
    return None

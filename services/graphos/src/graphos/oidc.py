"""Single sign-on through an OpenID Connect provider.

graphos is registered at the provider once, as a public client using PKCE.
People sign in with the Authorization Code flow, but **the browser stores
nothing**: graphos keeps the PKCE verifier and the nonce (table
``graphos_meta.oidc_pending``, single use, 5 minutes), exchanges the code
itself, validates the provider's tokens and answers with its own access and
refresh tokens, held in page memory.  The provider's tokens never reach the
browser.  A reload signs in again silently (``prompt=none``) while the
person's session at the provider lives: that is the single sign-on.

Programs may also send a provider access token straight to the API (from the
client-credentials grant, say); it is validated against the provider's
published keys, issuer and audience.

Either way the person or program becomes an :class:`Identity`: its subject,
the name recorded as consumer, its groups and roles, and the client it signed
in through.  Grants may name any of them (``oidc-sub:``, ``oidc-group:``,
``oidc-role:``, ``oidc-client:``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

log = logging.getLogger(__name__)

# Asymmetric algorithms only: never ``none``, never a shared secret.
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
METADATA_TTL_S = 3600.0
# A key id the cached key set lacks is looked up again at most this often.
KEYS_REFETCH_S = 60.0
PENDING_S = 300
LEEWAY_S = 60


class OidcError(Exception):
    """The provider could not be reached, or what it sent is not valid."""


@dataclass(frozen=True)
class Metadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None = None


@dataclass(frozen=True)
class Identity:
    """Who signed in at the provider."""

    subject: str                       # the provider's ``sub``
    consumer: str                      # recorded as the user name
    groups: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    client: str | None = None          # ``azp`` / ``client_id``

    def subjects(self) -> list[str]:
        """Every grant subject this identity matches."""
        out = [f"oidc-sub:{self.subject}"]
        out += [f"oidc-group:{g}" for g in self.groups]
        out += [f"oidc-role:{r}" for r in self.roles]
        if self.client:
            out.append(f"oidc-client:{self.client}")
        return out

    def claims(self) -> dict[str, Any]:
        return {"sub": self.subject, "name": self.consumer, "groups": list(self.groups),
                "roles": list(self.roles), "client": self.client}

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> Identity:
        return cls(
            subject=str(claims["sub"]), consumer=str(claims.get("name") or claims["sub"]),
            groups=tuple(claims.get("groups") or ()), roles=tuple(claims.get("roles") or ()),
            client=claims.get("client"),
        )


GRANT_SUBJECT_PREFIXES = ("user:", "oidc-sub:", "oidc-group:", "oidc-role:", "oidc-client:")


def pkce_pair() -> tuple[str, str]:
    """``(verifier, S256 challenge)``."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(v) for v in value if isinstance(v, str | int))
    return ()


def _claim(claims: dict[str, Any], path: str) -> Any:
    """A claim by name, or by dotted path (``realm_access.roles``)."""
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


@dataclass
class Provider:
    """One OpenID Connect provider: discovery, keys, code exchange, validation."""

    issuer: str
    client_id: str
    audience: str
    scopes: str = "openid profile email"
    consumer_claim: str = "preferred_username"
    groups_claim: str = "groups"
    roles_claim: str = "roles"
    timeout_s: float = 10.0
    transport: httpx.AsyncBaseTransport | None = None
    _metadata: Metadata | None = field(default=None, init=False, repr=False)
    _metadata_at: float = field(default=0.0, init=False, repr=False)
    _keys: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _keys_at: float = field(default=-1e9, init=False, repr=False)
    _lock: asyncio.Lock | None = field(default=None, init=False, repr=False)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport)

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            async with self._client() as http:
                resp = await http.get(url, headers={"Accept": "application/json"})
                resp.raise_for_status()
                document: dict[str, Any] = resp.json()
                return document
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError(f"the identity provider did not answer {url}: {exc}") from None

    async def metadata(self) -> Metadata:
        """The discovery document, cached for an hour."""
        if self._metadata is not None and time.monotonic() - self._metadata_at < METADATA_TTL_S:
            return self._metadata
        doc = await self._get_json(self.issuer.rstrip("/") + "/.well-known/openid-configuration")
        if doc.get("issuer") != self.issuer:
            raise OidcError(
                f"the discovery document names issuer {doc.get('issuer')!r}, not {self.issuer!r}",
            )
        try:
            self._metadata = Metadata(
                issuer=doc["issuer"],
                authorization_endpoint=doc["authorization_endpoint"],
                token_endpoint=doc["token_endpoint"],
                jwks_uri=doc["jwks_uri"],
                end_session_endpoint=doc.get("end_session_endpoint"),
            )
        except KeyError as exc:
            raise OidcError(f"the discovery document lacks {exc}") from None
        self._metadata_at = time.monotonic()
        return self._metadata

    async def _key(self, kid: str | None) -> Any:
        if kid in self._keys:
            return self._keys[kid]
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if kid not in self._keys and time.monotonic() - self._keys_at >= KEYS_REFETCH_S:
                meta = await self.metadata()
                keys: dict[str, Any] = {}
                for jwk in (await self._get_json(meta.jwks_uri)).get("keys", []):
                    if jwk.get("use", "sig") != "sig":
                        continue
                    try:
                        keys[jwk.get("kid")] = jwt.PyJWK(jwk).key
                    except jwt.PyJWTError:
                        continue                   # a key type this library does not use
                self._keys, self._keys_at = keys, time.monotonic()
        if kid not in self._keys:
            raise OidcError("the token was signed by a key the provider does not publish")
        return self._keys[kid]

    async def validate(
        self, token: str, *, audience: str, nonce: str | None = None,
    ) -> dict[str, Any]:
        """The claims of a token the provider signed for ``audience``."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise OidcError(f"not a token: {exc}") from None
        if header.get("alg") not in ALGORITHMS:
            raise OidcError(f"algorithm {header.get('alg')!r} is not accepted")
        key = await self._key(header.get("kid"))
        try:
            claims = jwt.decode(
                token, key, algorithms=ALGORITHMS, audience=audience, issuer=self.issuer,
                leeway=LEEWAY_S, options={"require": ["exp", "iat", "sub", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise OidcError(f"the token is not valid: {exc}") from None
        if nonce is not None and claims.get("nonce") != nonce:
            raise OidcError("the token's nonce does not match the sign-in")
        return claims

    def identity(self, claims: dict[str, Any]) -> Identity:
        client = claims.get("azp") or claims.get("client_id")
        consumer = _claim(claims, self.consumer_claim) or claims.get("preferred_username") \
            or claims.get("email") or client or claims["sub"]
        return Identity(
            subject=str(claims["sub"]),
            consumer=str(consumer),
            groups=_strings(_claim(claims, self.groups_claim)),
            roles=_strings(_claim(claims, self.roles_claim)),
            client=str(client) if client else None,
        )

    async def authorization_url(
        self, *, state: str, nonce: str, challenge: str, redirect_uri: str, silent: bool,
    ) -> str:
        meta = await self.metadata()
        params = {
            "response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
            "scope": self.scopes, "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256",
        }
        if silent:
            params["prompt"] = "none"
        sep = "&" if "?" in meta.authorization_endpoint else "?"
        return f"{meta.authorization_endpoint}{sep}{urlencode(params)}"

    async def exchange(self, *, code: str, verifier: str, redirect_uri: str) -> dict[str, Any]:
        """Trades the authorization code for the provider's tokens (as a public client)."""
        meta = await self.metadata()
        try:
            async with self._client() as http:
                resp = await http.post(meta.token_endpoint, data={
                    "grant_type": "authorization_code", "code": code,
                    "redirect_uri": redirect_uri, "client_id": self.client_id,
                    "code_verifier": verifier,
                }, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise OidcError(f"the identity provider did not answer: {exc}") from None
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != httpx.codes.OK or "id_token" not in body:
            raise OidcError(
                f"the identity provider refused the code: {body.get('error', resp.status_code)}",
            )
        tokens: dict[str, Any] = body
        return tokens

    async def sign_in(
        self, *, code: str, verifier: str, redirect_uri: str, nonce: str,
    ) -> tuple[Identity, str]:
        """Exchanges the code; returns who signed in and the provider's ID token."""
        tokens = await self.exchange(code=code, verifier=verifier, redirect_uri=redirect_uri)
        claims = await self.validate(tokens["id_token"], audience=self.client_id, nonce=nonce)
        # Groups and roles are often only in the access token: add them when it
        # is a token of this provider for graphos.
        access = tokens.get("access_token")
        if isinstance(access, str) and _looks_like_jwt(access):
            try:
                extra = await self.validate(access, audience=self.audience)
                for path in (self.groups_claim, self.roles_claim):
                    if _claim(claims, path) is None and _claim(extra, path) is not None:
                        claims = _merge(claims, path, _claim(extra, path))
            except OidcError:
                pass                            # an opaque or other-audience token: ignore
        return self.identity(claims), tokens["id_token"]

    async def end_session_url(
        self, post_logout_redirect_uri: str | None, id_token_hint: str | None = None,
    ) -> str | None:
        """Where to send the browser to sign out at the provider.  With the
        ID token as ``id_token_hint`` the provider signs out without asking."""
        meta = await self.metadata()
        if not meta.end_session_endpoint:
            return None
        params = {"client_id": self.client_id}
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        if post_logout_redirect_uri:
            params["post_logout_redirect_uri"] = post_logout_redirect_uri
        sep = "&" if "?" in meta.end_session_endpoint else "?"
        return f"{meta.end_session_endpoint}{sep}{urlencode(params)}"


def _merge(claims: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(json.dumps(claims))
    node = out
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return out


def _looks_like_jwt(token: str) -> bool:
    return len(token.split(".")) == len(("header", "payload", "signature"))


def provider_from(
    settings: Any, transport: httpx.AsyncBaseTransport | None = None,
) -> Provider | None:
    """The configured provider, or None without ``GRAPHOS_OIDC_ISSUER``."""
    if not settings.oidc_issuer:
        return None
    if not settings.oidc_client_id:
        raise RuntimeError("GRAPHOS_OIDC_ISSUER is set but GRAPHOS_OIDC_CLIENT_ID is not")
    return Provider(
        issuer=settings.oidc_issuer, client_id=settings.oidc_client_id,
        audience=settings.oidc_audience or settings.oidc_client_id,
        scopes=settings.oidc_scopes, consumer_claim=settings.oidc_consumer_claim,
        groups_claim=settings.oidc_groups_claim, roles_claim=settings.oidc_roles_claim,
        timeout_s=settings.oidc_http_timeout_s, transport=transport,
    )

"""A small OpenID Connect provider for tests, served through httpx's MockTransport.

It publishes a discovery document and an RSA key, "signs a person in" when a
test calls :meth:`FakeIdp.approve` (what the browser would do at the
provider), and checks the PKCE verifier when graphos exchanges the code.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.test/realms/acme"
CLIENT_ID = "graphos"
AUDIENCE = "graphos-api"


@dataclass
class FakeIdp:
    issuer: str = ISSUER
    key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )
    kid: str = "k1"
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    token_requests: list[dict[str, str]] = field(default_factory=list)
    discovery_issuer: str | None = None          # to publish a wrong issuer
    jwks_fetches: int = 0

    # -- what the provider publishes --

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def sign(self, claims: dict[str, Any], *, kid: str | None = None, alg: str = "RS256") -> str:
        now = int(time.time())
        body = {"iss": self.issuer, "iat": now, "exp": now + 300, **claims}
        return jwt.encode(body, self.key, algorithm=alg, headers={"kid": kid or self.kid})

    def access_token(self, **claims: Any) -> str:
        """A token a program would get (client credentials) for graphos's API."""
        return self.sign({"aud": AUDIENCE, **claims})

    # -- the browser part, done by the test --

    def approve(
        self, authorization_url: str, *, sub: str = "u-42", **claims: Any,
    ) -> tuple[str, str]:
        """The person signs in at the provider: ``(code, state)`` for the callback."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorization_url).query).items()}
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"query": query, "sub": sub, "claims": claims}
        return code, query["state"]

    # -- the HTTP side --

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            base = self.issuer
            return httpx.Response(200, json={
                "issuer": self.discovery_issuer or self.issuer,
                "authorization_endpoint": f"{base}/protocol/openid-connect/auth",
                "token_endpoint": f"{base}/protocol/openid-connect/token",
                "jwks_uri": f"{base}/protocol/openid-connect/certs",
                "end_session_endpoint": f"{base}/protocol/openid-connect/logout",
            })
        if path.endswith("/certs"):
            self.jwks_fetches += 1
            return httpx.Response(200, json=self.jwks())
        if path.endswith("/token"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            grant = self.codes.pop(form.get("code", ""), None)
            if grant is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            query = grant["query"]
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            same_redirect = form["redirect_uri"] == query["redirect_uri"]
            if challenge != query["code_challenge"] or not same_redirect:
                return httpx.Response(400, json={"error": "invalid_grant"})
            claims = dict(grant["claims"])
            nonce = claims.pop("nonce", query["nonce"])
            roles = claims.pop("roles", None)
            id_token = self.sign({"aud": form["client_id"], "sub": grant["sub"], "nonce": nonce,
                                  **claims})
            access = self.sign({"aud": AUDIENCE, "sub": grant["sub"], "azp": CLIENT_ID,
                                **({"roles": roles} if roles else {})})
            return httpx.Response(200, json={"id_token": id_token, "access_token": access,
                                             "token_type": "Bearer", "expires_in": 300})
        return httpx.Response(404)

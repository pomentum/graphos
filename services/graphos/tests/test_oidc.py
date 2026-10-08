"""The identity provider client: discovery, keys, validation, URLs.  No database."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import jwt
import pytest

from graphos.oidc import Identity, OidcError, Provider, pkce_pair

from .fake_idp import AUDIENCE, CLIENT_ID, ISSUER, FakeIdp


def _provider(idp: FakeIdp, **extra: object) -> Provider:
    return Provider(issuer=ISSUER, client_id=CLIENT_ID, audience=AUDIENCE,
                    transport=idp.transport(), **extra)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_authorization_url_asks_for_code_with_pkce() -> None:
    provider = _provider(FakeIdp())
    verifier, challenge = pkce_pair()
    assert len(verifier) >= 43 and challenge != verifier
    url = await provider.authorization_url(
        state="s", nonce="n", challenge=challenge, redirect_uri="https://ui.test/auth/callback",
        silent=True,
    )
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith(f"{ISSUER}/protocol/openid-connect/auth?")
    assert query == {
        "response_type": "code", "client_id": CLIENT_ID,
        "redirect_uri": "https://ui.test/auth/callback", "scope": "openid profile email",
        "state": "s", "nonce": "n", "code_challenge": challenge,
        "code_challenge_method": "S256", "prompt": "none",
    }


@pytest.mark.asyncio
async def test_a_discovery_document_for_another_issuer_is_refused() -> None:
    idp = FakeIdp(discovery_issuer="https://evil.test")
    with pytest.raises(OidcError, match="names issuer"):
        await _provider(idp).metadata()


@pytest.mark.asyncio
async def test_tokens_are_validated_against_the_published_keys() -> None:
    idp = FakeIdp()
    provider = _provider(idp)
    claims = await provider.validate(idp.access_token(sub="svc-1", azp="loader"), audience=AUDIENCE)
    assert claims["sub"] == "svc-1"
    for bad in (
        idp.sign({"sub": "x", "aud": "someone-else"}),                    # audience
        idp.sign({"sub": "x", "aud": AUDIENCE, "iss": "https://evil.test"}),   # issuer
        idp.sign({"sub": "x", "aud": AUDIENCE, "exp": 1}),                # expired
        FakeIdp().access_token(sub="x"),                                  # another key, same kid
        jwt.encode({"sub": "x", "aud": AUDIENCE, "iss": ISSUER}, "secret", algorithm="HS256",
                   headers={"kid": "k1"}),                                # shared secret
        "not-a-token",
    ):
        with pytest.raises(OidcError):
            await provider.validate(bad, audience=AUDIENCE)


@pytest.mark.asyncio
async def test_an_unknown_key_is_looked_up_at_most_once_a_minute() -> None:
    idp = FakeIdp()
    provider = _provider(idp)
    await provider.validate(idp.access_token(sub="a"), audience=AUDIENCE)
    for _ in range(3):
        with pytest.raises(OidcError, match="does not publish"):
            await provider.validate(idp.sign({"sub": "a", "aud": AUDIENCE}, kid="k9"),
                                    audience=AUDIENCE)
    assert idp.jwks_fetches == 1


def test_identity_reads_the_configured_claims() -> None:
    provider = Provider(issuer=ISSUER, client_id=CLIENT_ID, audience=AUDIENCE,
                        groups_claim="groups", roles_claim="realm_access.roles")
    who = provider.identity({
        "sub": "u-1", "preferred_username": "ada", "groups": ["analysts"],
        "realm_access": {"roles": ["graph-admin"]}, "azp": "graphos",
    })
    assert who == Identity(subject="u-1", consumer="ada", groups=("analysts",),
                           roles=("graph-admin",), client="graphos")
    assert who.subjects() == ["oidc-sub:u-1", "oidc-group:analysts", "oidc-role:graph-admin",
                              "oidc-client:graphos"]
    assert Identity.from_claims(who.claims()) == who
    # A program: no user name, its client id stands for it.
    assert provider.identity({"sub": "s-1", "azp": "loader"}).consumer == "loader"


@pytest.mark.asyncio
async def test_end_session_url() -> None:
    url = await _provider(FakeIdp()).end_session_url("https://ui.test/")
    assert url == (f"{ISSUER}/protocol/openid-connect/logout?client_id={CLIENT_ID}"
                   "&post_logout_redirect_uri=https%3A%2F%2Fui.test%2F")

"""Identity primitives: patterns, levels, passwords, tokens, throttling.

Pure Python; the flows against a database are in ``test_identity_db.py``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from graphos.identity import (
    Level,
    PasswordPolicyError,
    Principal,
    Signer,
    Throttle,
    TokenError,
    User,
    check_policy,
    hash_password,
    matches,
    valid_pattern,
    verify_password,
)


def _user(**extra: object) -> User:
    fields = {
        "name": "ada", "kind": "person", "must_change": False, "disabled": False,
        "token_version": 3, "created_at": datetime.now(UTC), "created_by": None,
        "last_login_at": None,
    }
    return User(**{**fields, **extra})  # type: ignore[arg-type]


def _signer(ttl: int = 900, issuer: str = "graphos") -> Signer:
    signer = Signer(issuer=issuer, ttl_s=ttl)
    signer.use_key("k1", ec.generate_private_key(ec.SECP256R1()))
    return signer


def test_patterns_match_names_literally_except_star() -> None:
    assert matches("*", "anything")
    assert matches("acme", "acme") and not matches("acme", "acme2")
    assert matches("sol_*", "sol_1") and matches("sol_*", "sol_") and not matches("sol_*", "xsol_1")
    assert matches("*_prod", "a_prod") and not matches("*_prod", "a_dev")
    assert valid_pattern("sol_*") and valid_pattern("*") and valid_pattern("acme")
    assert not valid_pattern("a.b") and not valid_pattern("") and not valid_pattern("a-b")


def test_levels_are_ordered_and_the_highest_match_wins() -> None:
    p = Principal.static("u", tenants={"*": "read", "acme": "maintain", "ac*": "write"})
    assert p.level("acme") is Level.MAINTAIN
    assert p.level("acorn") is Level.WRITE
    assert p.level("zeta") is Level.READ
    assert p.has("acme", Level.WRITE) and not p.has("acme", Level.OWNER)
    assert Principal.static("n").level("acme") is None
    admin = Principal.static("a", admin=True)
    assert admin.level("x") is Level.OWNER and admin.can_create("x")


def test_create_tenants_is_by_pattern() -> None:
    p = Principal.static("svc", create_tenants=["sol_*"])
    assert p.can_create("sol_9") and not p.can_create("acme")
    assert p.visible(["sol_9", "acme"]) == []          # creating is not reading


def test_passwords_hash_with_a_salt_and_verify() -> None:
    a, b = hash_password("correct horse"), hash_password("correct horse")
    assert a != b and a.startswith("scrypt$")
    assert verify_password("correct horse", a) and not verify_password("wrong", a)
    assert not verify_password("x", "not-a-hash") and not verify_password("x", "md5$1$2$3$4$5")


def test_password_policy() -> None:
    check_policy("ada", "a long enough one", min_length=12)
    with pytest.raises(PasswordPolicyError, match="at least 12"):
        check_policy("ada", "short", min_length=12)
    with pytest.raises(PasswordPolicyError, match="user name"):
        check_policy("administrator", "Administrator", min_length=12)
    with pytest.raises(PasswordPolicyError, match="differ"):
        check_policy("ada", "same old password", min_length=12, current="same old password")


def test_tokens_carry_the_user_and_version_and_verify() -> None:
    signer = _signer()
    token, ttl = signer.issue(_user())
    assert ttl == 900
    claims = signer.verify(token)
    assert (claims["sub"], claims["ver"]) == ("ada", 3)
    assert (claims["aud"], claims["iss"]) == ("graphos", "graphos")
    assert jwt.get_unverified_header(token)["alg"] == "ES256"


def test_tokens_from_elsewhere_are_refused() -> None:
    token, _ = _signer().issue(_user())
    with pytest.raises(TokenError):
        _signer().verify(token)                       # another key, same kid
    with pytest.raises(TokenError):
        _signer(issuer="other").verify(_signer().issue(_user())[0])
    signer = _signer()
    other_issuer = Signer(issuer="other", ttl_s=900)
    other_issuer._signing = signer._signing           # same key, wrong iss
    other_issuer._public = signer._public
    with pytest.raises(TokenError):
        signer.verify(other_issuer.issue(_user())[0])
    with pytest.raises(TokenError):
        signer.verify("not.a.token")
    # Unsigned or symmetric tokens never pass.
    forged = jwt.encode({"sub": "ada", "ver": 3, "aud": "graphos", "iss": "graphos",
                         "iat": int(time.time()), "exp": int(time.time()) + 60},
                        "secret", algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(TokenError):
        signer.verify(forged)


def test_expired_tokens_are_refused() -> None:
    signer = _signer(ttl=60)
    signer.ttl_s = -120                                # already past the 30 s leeway
    token, _ = signer.issue(_user())
    with pytest.raises(TokenError):
        signer.verify(token)


def test_throttle_counts_attempts_per_address_per_minute() -> None:
    throttle = Throttle(per_min=2)
    assert throttle.admit("a", now=100) and throttle.admit("a", now=101)
    assert not throttle.admit("a", now=102)
    assert throttle.admit("b", now=102)                # another address
    assert throttle.admit("a", now=161)                # a minute later

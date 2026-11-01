"""Identity, beyond the primitives: tokens at their edges, the access store,
sign-in checks, browser sessions.  No database: a scripted pool stands in
(``_signin_fakes``)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from graphos import identity, vault
from graphos.identity import (
    REFUSED_MESSAGE,
    AccessStore,
    AuthError,
    Level,
    Principal,
    SessionError,
    Signer,
    Throttle,
    TokenError,
    close_session,
    open_session,
    rotate_session,
    valid_user_name,
)
from graphos.oidc import Identity

from ._signin_fakes import FakePool, grant_row, make_user, user_row


@pytest.fixture(autouse=True)
def _cheap_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hashes made here cost little; verifying reads the cost from the hash.
    monkeypatch.setattr(identity, "_SCRYPT_N", 2**4)


def _cheap_hash(password: str) -> str:
    salt = b"0123456789abcdef"
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16, r=8, p=1, dklen=32)
    b64 = base64.urlsafe_b64encode
    return f"scrypt$16$8$1${b64(salt).decode()}${b64(digest).decode()}"


# ── Levels, names, principals ────────────────────────────────────────────


def test_a_level_parses_from_its_name_in_any_case_and_refuses_others() -> None:
    assert Level.parse("Maintain") is Level.MAINTAIN
    assert Level.parse(Level.OWNER) is Level.OWNER
    with pytest.raises(ValueError, match="not a level"):
        Level.parse("superuser")
    with pytest.raises(ValueError, match="not a level"):
        Level.parse("admin")          # a global right, not a tenant level


@pytest.mark.parametrize(
    ("name", "ok"),
    [("alice", True), ("svc.loader-1", True), ("a", True), ("Alice", False),
     ("1alice", False), ("", False), ("al ice", False), ("a" * 64, False)],
)
def test_user_names_are_lowercase_and_start_with_a_letter(name: str, ok: bool) -> None:  # noqa: FBT001
    assert valid_user_name(name) is ok


def test_visible_tenants_respect_the_level_asked_for() -> None:
    p = Principal.static("bob", tenants={"acme": "read", "beta": "write"})
    assert p.visible(["beta", "acme", "zeta"]) == ["acme", "beta"]
    assert p.visible(["beta", "acme"], at_least=Level.WRITE) == ["beta"]


@pytest.mark.parametrize(
    "stored",
    [
        "scrypt$notanumber$8$1$c2FsdA==$ZGlnZXN0",
        "scrypt$16384$8$1$!!$ZGlnZXN0",              # salt not base64
        "scrypt$3$8$1$c2FsdA==$ZGlnZXN0",            # cost not a power of two
        "scrypt$99999999999999999999$8$1$c2FsdA==$ZGlnZXN0",
        "scrypt$16$8$1$c2FsdA==",                    # a field missing
        "",
    ],
)
def test_a_damaged_stored_hash_refuses_the_password_without_raising(stored: str) -> None:
    assert identity.verify_password("anything", stored) is False


# ── Tokens ───────────────────────────────────────────────────────────────


def _keyed_signer(issuer: str = "graphos") -> tuple[Signer, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    signer = Signer(issuer=issuer, ttl_s=900)
    signer.use_key("k1", key)
    return signer, key


def _forge(key: Any, *, kid: Any = "k1", alg: str = "ES256", **claims: Any) -> str:
    now = int(time.time())
    body = {"iss": "graphos", "aud": "graphos", "iat": now, "nbf": now, "exp": now + 300,
            "sub": "alice", "ver": 1, **claims}
    body = {k: v for k, v in body.items() if v is not None}
    return jwt.encode(body, key, algorithm=alg, headers={"kid": kid})


def test_a_token_expired_within_the_leeway_still_verifies() -> None:
    signer, key = _keyed_signer()
    now = int(time.time())
    claims = signer.verify(_forge(key, iat=now - 900, nbf=now - 900, exp=now - 20))
    assert claims["sub"] == "alice"


def test_a_token_expired_beyond_the_leeway_is_refused() -> None:
    signer, key = _keyed_signer()
    now = int(time.time())
    with pytest.raises(TokenError, match="expired"):
        signer.verify(_forge(key, iat=now - 900, nbf=now - 900, exp=now - 40))


def test_a_token_not_yet_valid_beyond_the_leeway_is_refused() -> None:
    signer, key = _keyed_signer()
    now = int(time.time())
    with pytest.raises(TokenError):
        signer.verify(_forge(key, nbf=now + 120, exp=now + 600))
    with pytest.raises(TokenError):
        signer.verify(_forge(key, iat=now + 120, nbf=None, exp=now + 600))


@pytest.mark.parametrize("missing", ["exp", "iat", "sub"])
def test_a_token_without_a_required_claim_is_refused(missing: str) -> None:
    signer, key = _keyed_signer()
    with pytest.raises(TokenError):
        signer.verify(_forge(key, **{missing: None}))


def test_a_token_for_another_audience_or_issuer_is_refused() -> None:
    signer, key = _keyed_signer()
    with pytest.raises(TokenError, match="[Aa]udience"):
        signer.verify(_forge(key, aud="someone-else"))
    with pytest.raises(TokenError, match="[Ii]ssuer"):
        signer.verify(_forge(key, iss="https://evil.example"))
    # An audience list naming graphos is a token for graphos.
    assert signer.verify(_forge(key, aud=["other", "graphos"]))["sub"] == "alice"


def test_a_token_naming_no_known_key_is_refused_before_any_signature_check() -> None:
    signer, key = _keyed_signer()
    with pytest.raises(TokenError, match="unknown signing key"):
        signer.verify(_forge(key, kid="k2"))
    _, body, sig = _forge(key).split(".")
    numeric = base64.urlsafe_b64encode(b'{"alg":"ES256","kid":7}').rstrip(b"=").decode()
    with pytest.raises(TokenError):
        signer.verify(f"{numeric}.{body}.{sig}")
    no_kid = jwt.encode({"sub": "alice"}, key, algorithm="ES256")
    with pytest.raises(TokenError, match="unknown signing key"):
        signer.verify(no_kid)


def test_an_unsigned_token_is_refused() -> None:
    signer, _ = _keyed_signer()
    header = base64.urlsafe_b64encode(b'{"alg":"none","kid":"k1"}').rstrip(b"=").decode()
    now = int(time.time())
    payload = base64.urlsafe_b64encode(json.dumps({
        "iss": "graphos", "aud": "graphos", "iat": now, "exp": now + 60, "sub": "admin",
    }).encode()).rstrip(b"=").decode()
    with pytest.raises(TokenError):
        signer.verify(f"{header}.{payload}.")


def test_a_token_with_a_tampered_payload_is_refused() -> None:
    signer, key = _keyed_signer()
    head, _, sig = _forge(key).split(".")
    body = base64.urlsafe_b64encode(json.dumps({
        "iss": "graphos", "aud": "graphos", "iat": int(time.time()),
        "exp": int(time.time()) + 60, "sub": "graphos", "ver": 1,
    }).encode()).rstrip(b"=").decode()
    with pytest.raises(TokenError):
        signer.verify(f"{head}.{body}.{sig}")


def test_an_mcp_token_is_marked_read_only_and_bound_to_its_session() -> None:
    signer, _ = _keyed_signer()
    token, _ = signer.issue(make_user(token_version=4), read_only=True, session_until=1_900_000_000)
    claims = signer.verify(token)
    assert (claims["ro"], claims["sxp"], claims["ver"]) == (True, 1_900_000_000, 4)
    plain = signer.verify(signer.issue(make_user())[0])
    assert "ro" not in plain and "sxp" not in plain
    # Two tokens issued at once are still told apart.
    assert signer.verify(signer.issue(make_user())[0])["jti"] != plain["jti"]


def test_a_single_sign_on_token_carries_the_identity() -> None:
    signer, _ = _keyed_signer()
    who = Identity(subject="u-1", consumer="alice", groups=("analysts",))
    claims = signer.verify(signer.issue_for(who, session_until=1_900_000_000)[0])
    assert claims["sub"] == "oidc:u-1"
    assert Identity.from_claims(claims["idn"]) == who
    assert claims["sxp"] == 1_900_000_000
    assert "sxp" not in signer.verify(signer.issue_for(who)[0])


def test_nothing_is_signed_before_a_key_is_loaded() -> None:
    with pytest.raises(RuntimeError, match="no signing key"):
        Signer(issuer="graphos", ttl_s=900).issue(make_user())
    with pytest.raises(TokenError):
        Signer(issuer="graphos", ttl_s=900).verify(_forge(ec.generate_private_key(ec.SECP256R1())))


def _pem(key: Any) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


async def test_a_signing_key_file_signs_and_names_its_kid_after_its_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "signing.pem"
    path.write_bytes(_pem(ec.generate_private_key(ec.SECP256R1())))
    signer = Signer(issuer="graphos", ttl_s=900)
    await signer.load(FakePool(), path)
    token, _ = signer.issue(make_user())
    assert jwt.get_unverified_header(token)["kid"].startswith("file-")
    assert signer.verify(token)["sub"] == "alice"


async def test_a_signing_key_file_that_is_not_an_ec_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "signing.pem"
    path.write_bytes(_pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)))
    with pytest.raises(RuntimeError, match="EC P-256"):
        await Signer(issuer="graphos", ttl_s=900).load(FakePool(), path)


def _vault() -> vault.Vault:
    return vault.Vault({1: b"\x01" * 32})


async def test_the_first_start_generates_a_key_and_stores_it_sealed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vault, "current", _vault)
    pool = FakePool()
    signer = Signer(issuer="graphos", ttl_s=900)
    await signer.load(pool)
    (insert,) = pool.ran("INSERT INTO graphos_meta.signing_keys")
    kid, stored = insert.args
    assert vault.Vault.is_sealed(stored) and "PRIVATE KEY" not in stored
    token, _ = signer.issue(make_user())
    assert jwt.get_unverified_header(token)["kid"] == kid
    assert pool.transactions == ["commit"]


async def test_the_newest_key_signs_and_older_unretired_keys_still_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vault, "current", _vault)
    new, old = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    pool = FakePool().on("SELECT kid, private_pem", [
        {"kid": "new", "private_pem": _pem(new).decode()},       # stored in clear
        {"kid": "old", "private_pem": _vault().seal(_pem(old).decode(), "signing_key:old")},
    ])
    signer = Signer(issuer="graphos", ttl_s=900)
    await signer.load(pool)
    assert jwt.get_unverified_header(signer.issue(make_user())[0])["kid"] == "new"
    assert signer.verify(_forge(old, kid="old"))["sub"] == "alice"
    # The key stored in clear is sealed now that a master key is set; the other stays.
    (reseal,) = pool.ran("UPDATE graphos_meta.signing_keys")
    assert reseal.args[0] == "new" and vault.Vault.is_sealed(reseal.args[1])
    assert not pool.ran("INSERT INTO graphos_meta.signing_keys")


async def test_a_key_sealed_with_a_master_key_this_process_lacks_stops_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sealed = vault.Vault({2: b"\x02" * 32}).seal("pem", "signing_key:k")
    monkeypatch.setattr(vault, "current", _vault)
    pool = FakePool().on("SELECT kid, private_pem", [{"kid": "k", "private_pem": sealed}])
    with pytest.raises(vault.VaultError):
        await Signer(issuer="graphos", ttl_s=900).load(pool)


# ── The access store ─────────────────────────────────────────────────────


def _store(*users: dict[str, Any], grants: list[dict[str, Any]] | None = None) -> (
        tuple[AccessStore, FakePool]):
    pool = FakePool()
    pool.on("FROM graphos_meta.users;", list(users))
    pool.on("FROM graphos_meta.grants;", grants or [])
    return AccessStore(pool), pool  # type: ignore[arg-type]


async def test_the_store_builds_principals_from_users_and_their_grants() -> None:
    store, _ = _store(
        user_row("alice"), user_row("bob", must_change=True), user_row("carol", disabled=True),
        grants=[
            grant_row("user:alice", "acme", "write"), grant_row("user:alice", "sol_*", "read"),
            grant_row("user:alice", "sol_*", "create_tenants"),
            grant_row("user:bob", "*", "admin"), grant_row("user:carol", "acme", "owner"),
        ],
    )
    await store.load()
    assert [u.name for u in store.users()] == ["alice", "bob", "carol"]
    alice = store.principal("alice")
    assert alice is not None and not alice.admin
    assert alice.level("acme") is Level.WRITE and alice.level("sol_1") is Level.READ
    assert alice.can_create("sol_2") and not alice.can_create("acme")
    bob = store.principal("bob")
    assert bob is not None and bob.admin and bob.must_change
    assert store.principal("carol") is None             # disabled
    assert store.principal("nobody") is None


async def test_grants_for_a_tenant_leave_out_global_rights() -> None:
    store, _ = _store(grants=[
        grant_row("user:alice", "acme", "read"), grant_row("user:bob", "*", "admin"),
        grant_row("user:carol", "ac*", "create_tenants"), grant_row("user:dave", "*", "write"),
    ])
    await store.load()
    assert [(g.subject, g.level) for g in store.grants(tenant="acme")] == [
        ("user:alice", "read"), ("user:dave", "write"),
    ]
    assert [g.level for g in store.grants("user:bob")] == ["admin"]
    assert len(store.grants()) == 4  # noqa: PLR2004


async def test_a_single_sign_on_principal_gathers_its_subject_group_and_client_grants() -> None:
    store, _ = _store(grants=[
        grant_row("oidc-sub:u-1", "mine", "owner"),
        grant_row("oidc-group:analysts", "acme", "read"),
        grant_row("oidc-client:loader", "acme", "write"),
        grant_row("oidc-group:others", "secret", "owner"),
    ])
    await store.load()
    who = Identity(subject="u-1", consumer="alice", groups=("analysts",), client="loader")
    p = store.principal_for(who, kind="service")
    assert (p.subject, p.via, p.kind, p.consumer) == ("oidc-sub:u-1", "oidc", "service", "alice")
    assert p.level("acme") is Level.WRITE and p.level("mine") is Level.OWNER
    assert p.level("secret") is None


async def test_creating_a_person_makes_them_change_a_generated_password() -> None:
    store, pool = _store()
    pool.on("INSERT INTO graphos_meta.users",
            lambda *a: user_row(a[0], kind=a[1], must_change=a[3]))
    person, secret = await store.create_user("alice", "person", by="admin")
    assert person.must_change and len(secret) >= 24  # noqa: PLR2004
    service, _ = await store.create_user("svc", "service", by="admin")
    assert not service.must_change
    chosen, given = await store.create_user("bob", "person", by="admin", password="chosen-one-1",
                                            must_change=False)
    assert given == "chosen-one-1" and not chosen.must_change
    stored = pool.ran("INSERT INTO graphos_meta.users")[-1].args[2]
    assert identity.verify_password("chosen-one-1", stored) and "chosen-one-1" not in stored
    assert len(pool.ran("pg_notify")) == 3  # noqa: PLR2004


async def test_creating_a_user_that_exists_is_refused() -> None:
    store, pool = _store()
    pool.on("INSERT INTO graphos_meta.users", None)
    with pytest.raises(ValueError, match="already exists"):
        await store.create_user("alice", "person", by="admin")
    assert pool.transactions == ["rollback"] and not pool.ran("pg_notify")


async def test_setting_a_password_ends_every_session_and_older_token() -> None:
    store, pool = _store(user_row("alice"))
    pool.on("UPDATE graphos_meta.users SET password_hash", "UPDATE 1")
    await store.set_password("alice", "new-password-1", must_change=False)
    (update,) = pool.ran("SET password_hash")
    assert "token_version = token_version + 1" in update.sql
    assert identity.verify_password("new-password-1", update.args[1])
    assert pool.ran("UPDATE graphos_meta.sessions SET revoked_at")[0].args == ("alice",)
    assert pool.ran("pg_notify") and pool.transactions == ["commit"]


async def test_setting_the_password_of_an_unknown_user_changes_nothing() -> None:
    store, pool = _store()
    pool.on("UPDATE graphos_meta.users SET password_hash", "UPDATE 0")
    with pytest.raises(KeyError):
        await store.set_password("ghost", "new-password-1", must_change=False)
    assert not pool.ran("graphos_meta.sessions") and pool.transactions == ["rollback"]


async def test_disabling_ends_sessions_and_enabling_does_not() -> None:
    store, pool = _store(user_row("alice"))
    pool.on("SET disabled", "UPDATE 1")
    await store.set_disabled("alice", True)
    assert len(pool.ran("graphos_meta.sessions")) == 1
    await store.set_disabled("alice", False)
    assert len(pool.ran("graphos_meta.sessions")) == 1
    assert all("token_version + 1" in c.sql for c in pool.ran("SET disabled"))
    with pytest.raises(KeyError):
        await AccessStore(FakePool().on("SET disabled", "UPDATE 0")).set_disabled("x", True)  # type: ignore[arg-type]


async def test_deleting_a_user_takes_their_grants_sessions_and_preferences() -> None:
    store, pool = _store()
    pool.on("DELETE FROM graphos_meta.users", "DELETE 1")
    await store.delete_user("alice")
    assert pool.ran("DELETE FROM graphos_meta.grants")[0].args == ("user:alice",)
    assert pool.ran("DELETE FROM graphos_meta.user_preferences")[0].args == ("user:alice",)
    assert pool.ran("graphos_meta.sessions")[0].args == ("alice",)
    store2, pool2 = _store()
    pool2.on("DELETE FROM graphos_meta.users", "DELETE 0")
    with pytest.raises(KeyError):
        await store2.delete_user("ghost")


async def test_grant_and_revoke_say_whether_anything_changed() -> None:
    store, pool = _store()
    pool.on("INSERT INTO graphos_meta.grants", "INSERT 0 1")
    pool.on("DELETE FROM graphos_meta.grants", "DELETE 1")
    assert await store.grant("user:alice", "acme", "read", by="admin")
    assert await store.revoke("user:alice", "acme", "read")
    store2, pool2 = _store()
    pool2.on("INSERT INTO graphos_meta.grants", "INSERT 0 0")
    pool2.on("DELETE FROM graphos_meta.grants", "DELETE 0")
    assert not await store2.grant("user:alice", "acme", "read", by="admin")
    assert not await store2.revoke("user:alice", "acme", "read")


async def test_dropping_a_tenant_keeps_global_rights() -> None:
    store, pool = _store()
    await store.drop_tenant("acme")
    (drop,) = pool.ran("DELETE FROM graphos_meta.grants")
    assert drop.args == ("acme",) and "NOT IN ('create_tenants', 'admin')" in drop.sql


# ── Checking a password ──────────────────────────────────────────────────


def _signin_store(
    row: dict[str, Any] | None, *users: dict[str, Any],
) -> tuple[AccessStore, FakePool]:
    store, pool = _store(*users)
    pool.rules.insert(0, ("SELECT password_hash", row))
    return store, pool


async def _check(store: AccessStore, name: str = "alice", password: str = "right-password") -> Any:
    return await store.check_password(name, password, max_failures=3, lockout_s=60)


async def test_the_right_password_signs_in_and_clears_the_failure_count() -> None:
    store, pool = _signin_store(
        {"password_hash": _cheap_hash("right-password"), "disabled": False, "locked": None},
        user_row("alice"),
    )
    await store.load()
    user = await _check(store)
    assert user.name == "alice"
    (reset,) = pool.ran("SET failed_logins = 0")
    assert "last_login_at = now()" in reset.sql


async def test_a_wrong_password_counts_towards_the_lockout_and_is_refused() -> None:
    store, pool = _signin_store(
        {"password_hash": _cheap_hash("right-password"), "disabled": False, "locked": False},
    )
    pool.rules.insert(0, ("RETURNING failed_logins", 2))
    with pytest.raises(AuthError) as refused:
        await _check(store, password="wrong-password")
    assert str(refused.value) == REFUSED_MESSAGE
    (count,) = pool.ran("RETURNING failed_logins")
    assert count.args == ("alice", 3, 60.0)


@pytest.mark.parametrize(
    "row",
    [
        None,                                                                      # unknown
        {"password_hash": _cheap_hash("right-password"), "disabled": True, "locked": False},
        {"password_hash": _cheap_hash("right-password"), "disabled": False, "locked": True},
    ],
    ids=["unknown", "disabled", "locked"],
)
async def test_unknown_disabled_and_locked_accounts_are_refused_alike_even_with_the_password(
    row: dict[str, Any] | None,
) -> None:
    store, pool = _signin_store(row, user_row("alice"))
    await store.load()
    with pytest.raises(AuthError) as refused:
        await _check(store)
    assert str(refused.value) == REFUSED_MESSAGE
    # Nothing is counted or reset: a locked account is not extended, a disabled one not touched.
    assert not pool.ran("UPDATE graphos_meta.users")


async def test_an_unknown_user_still_costs_a_password_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[str] = []
    monkeypatch.setattr(identity, "verify_password", lambda pw, stored: checked.append(stored))
    store, _ = _signin_store(None)
    with pytest.raises(AuthError):
        await _check(store, name="nobody")
    assert checked == [identity._DUMMY_HASH]


async def test_a_user_created_on_another_replica_is_loaded_at_sign_in() -> None:
    store, pool = _signin_store(
        {"password_hash": _cheap_hash("right-password"), "disabled": False, "locked": False},
        user_row("alice"),
    )
    assert store.user("alice") is None                       # not loaded yet
    assert (await _check(store)).name == "alice"
    assert pool.ran("FROM graphos_meta.users;")


async def test_a_user_deleted_while_signing_in_is_refused() -> None:
    store, _ = _signin_store(
        {"password_hash": _cheap_hash("right-password"), "disabled": False, "locked": False},
    )
    with pytest.raises(AuthError):
        await _check(store)


# ── Bootstrap ────────────────────────────────────────────────────────────


async def test_the_bootstrap_admin_gets_the_default_password_and_must_change_it() -> None:
    store, pool = _store(user_row("graphos", must_change=True))
    pool.rules.insert(0, ("SELECT EXISTS", False))
    assert await store.bootstrap(None) == "default"
    (insert,) = pool.ran("INSERT INTO graphos_meta.users")
    assert insert.args[0] == "graphos" and "must_change, created_by) " in insert.sql
    assert identity.verify_password("graphos", insert.args[1])
    assert pool.ran("INSERT INTO graphos_meta.grants")[0].args == ("user:graphos",)
    assert store.first_password_unchanged()


async def test_the_bootstrap_password_can_come_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "admin"
    path.write_text("  file-password-1\n", encoding="utf-8")
    store, pool = _store()
    pool.rules.insert(0, ("SELECT EXISTS", False))
    assert await store.bootstrap(path) == "file"
    stored = pool.ran("INSERT INTO graphos_meta.users")[0].args[1]
    assert identity.verify_password("file-password-1", stored)


async def test_an_empty_bootstrap_password_file_stops_the_start(tmp_path: Path) -> None:
    path = tmp_path / "admin"
    path.write_text("   \n", encoding="utf-8")
    store, pool = _store()
    pool.rules.insert(0, ("SELECT EXISTS", False))
    with pytest.raises(RuntimeError, match="is empty"):
        await store.bootstrap(path)
    assert not pool.ran("INSERT") and pool.transactions == ["rollback"]


async def test_bootstrap_does_nothing_once_a_user_exists(tmp_path: Path) -> None:
    store, pool = _store(user_row("graphos", must_change=False))
    pool.rules.insert(0, ("SELECT EXISTS", True))
    assert await store.bootstrap(tmp_path / "ignored") is None
    assert not pool.ran("INSERT")
    await store.load()
    assert not store.first_password_unchanged()


async def test_a_disabled_bootstrap_admin_is_not_reported_as_unchanged() -> None:
    store, _ = _store(user_row("graphos", must_change=True, disabled=True))
    await store.load()
    assert not store.first_password_unchanged()


# ── Reloading ────────────────────────────────────────────────────────────


async def test_notifications_coalesce_into_one_reload_and_failures_are_swallowed() -> None:
    store, pool = _store(user_row("alice"))
    store.on_notify()
    store.on_notify()
    assert store._reload_task is not None
    await store._reload_task
    assert len(pool.ran("FROM graphos_meta.users;")) == 1
    assert store.user("alice") is not None

    def boom(*_: Any) -> Any:
        raise OSError("connection lost")

    pool.rules.insert(0, ("FROM graphos_meta.users;", boom))
    store.on_notify()
    await store._reload_task                                   # does not raise
    assert store.user("alice") is not None                     # the last snapshot stays


async def test_the_periodic_reload_starts_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity, "RELOAD_S", 0.0)
    store, pool = _store(user_row("alice"))
    store.start()
    for _ in range(50):
        await asyncio.sleep(0)
        if pool.ran("FROM graphos_meta.users;"):
            break
    await store.stop()
    assert pool.ran("FROM graphos_meta.users;")
    assert store._loop_task is not None and store._loop_task.cancelled()


# ── Sessions ─────────────────────────────────────────────────────────────


async def test_a_session_ends_no_later_than_the_cap_it_was_given() -> None:
    pool = FakePool()
    cap = datetime.now(UTC) + timedelta(minutes=5)
    opened = await open_session(pool, "alice", idle_s=3600, max_s=43200, client="c1", until=cap)  # type: ignore[arg-type]
    assert opened.until == cap
    (insert,) = pool.ran("INSERT INTO graphos_meta.sessions")
    args = insert.args
    assert args[1] == "alice" and args[4] == cap and args[8] == "c1"
    # Only a hash of the refresh token is stored.
    assert opened.token not in args and args[2] == hashlib.sha256(opened.token.encode()).hexdigest()
    later = await open_session(pool, "alice", idle_s=3600, max_s=60,  # type: ignore[arg-type]
                               until=datetime.now(UTC) + timedelta(days=1))
    assert later.until < datetime.now(UTC) + timedelta(seconds=61)


async def test_a_single_sign_on_session_keeps_its_claims_and_seals_the_id_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vault, "current", _vault)
    pool = FakePool()
    who = Identity(subject="u-1", consumer="alice")
    await open_session(pool, "oidc:u-1", idle_s=60, max_s=600, identity=who, id_token="idt")  # type: ignore[arg-type]
    args = pool.ran("INSERT INTO graphos_meta.sessions")[0].args
    assert args[5] == "oidc" and json.loads(args[6]) == who.claims()
    assert vault.Vault.is_sealed(args[7])
    assert _vault().open(args[7], vault.session_context(args[0])) == "idt"


def _session_row(**extra: Any) -> dict[str, Any]:
    return {
        "id": 1, "family": "fam-1", "user_name": "alice", "rotated_at": None,
        "revoked_at": None, "kind": "native", "claims": None, "id_token": None, "client": None,
        "expired": False, "absolute_expires_at": datetime(2030, 1, 1, tzinfo=UTC),
        "just_rotated": None, **extra,
    }


def _session_pool(row: dict[str, Any] | None, *, family_live: bool = False) -> FakePool:
    return (FakePool().on("FROM graphos_meta.sessions WHERE refresh_hash", row)
            .on("SELECT EXISTS", family_live))


async def test_a_valid_refresh_token_rotates() -> None:
    pool = _session_pool(_session_row())
    renewed = await rotate_session(pool, "tok", idle_s=600)  # type: ignore[arg-type]
    assert renewed.user == "alice" and renewed.refresh_token and renewed.identity is None
    assert renewed.until == datetime(2030, 1, 1, tzinfo=UTC)
    assert pool.ran("SET rotated_at = now()")[0].args == (1,)
    insert = pool.ran("INSERT INTO graphos_meta.sessions")[0]
    assert insert.args[0] == "fam-1"
    assert insert.args[2] == hashlib.sha256(renewed.refresh_token.encode()).hexdigest()
    assert pool.transactions == ["commit"]


async def test_an_unknown_refresh_token_is_refused() -> None:
    with pytest.raises(SessionError, match="unknown"):
        await rotate_session(_session_pool(None), "tok", idle_s=600)  # type: ignore[arg-type]


async def test_a_session_is_renewed_only_by_the_client_it_belongs_to() -> None:
    pool = _session_pool(_session_row(client="c1"))
    with pytest.raises(SessionError, match="/oauth/token"):
        await rotate_session(pool, "tok", idle_s=600)  # type: ignore[arg-type]
    with pytest.raises(SessionError, match="not this client's"):
        await rotate_session(_session_pool(_session_row()), "tok", idle_s=600, client="c1")  # type: ignore[arg-type]
    with pytest.raises(SessionError):
        await rotate_session(pool, "tok", idle_s=600, client="c2")  # type: ignore[arg-type]
    # Presented in the wrong place, it neither rotates nor revokes.
    assert not pool.ran("UPDATE graphos_meta.sessions")


async def test_a_reused_refresh_token_revokes_its_whole_family_and_the_revocation_commits() -> None:
    pool = _session_pool(_session_row(rotated_at=datetime(2026, 1, 1, tzinfo=UTC),
                                      just_rotated=False))
    with pytest.raises(SessionError, match="revoked"):
        await rotate_session(pool, "tok", idle_s=600)  # type: ignore[arg-type]
    (revoke,) = pool.ran("SET revoked_at = now()")
    assert revoke.args == ("fam-1",) and "WHERE family = $1" in revoke.sql
    assert pool.transactions == ["commit"]
    assert not pool.ran("INSERT INTO graphos_meta.sessions")


async def test_a_token_rotated_a_moment_ago_renews_without_rotating_while_its_family_lives(
) -> None:
    row = _session_row(rotated_at=datetime.now(UTC), just_rotated=True)
    pool = _session_pool(row, family_live=True)
    renewed = await rotate_session(pool, "tok", idle_s=600)  # type: ignore[arg-type]
    assert renewed.refresh_token is None and renewed.user == "alice"
    assert not pool.ran("UPDATE graphos_meta.sessions") and not pool.ran("INSERT")


async def test_a_token_rotated_a_moment_ago_is_reuse_once_the_family_has_ended() -> None:
    row = _session_row(rotated_at=datetime.now(UTC), just_rotated=True)
    pool = _session_pool(row, family_live=False)
    with pytest.raises(SessionError, match="revoked"):
        await rotate_session(pool, "tok", idle_s=600)  # type: ignore[arg-type]
    assert pool.ran("SET revoked_at = now()")


@pytest.mark.parametrize(
    "extra",
    [{"expired": True}, {"revoked_at": datetime(2026, 1, 1, tzinfo=UTC)},
     {"revoked_at": datetime(2026, 1, 1, tzinfo=UTC), "just_rotated": True}],
    ids=["expired", "revoked", "revoked-within-grace"],
)
async def test_an_ended_session_is_refused_and_not_rotated(extra: dict[str, Any]) -> None:
    pool = _session_pool(_session_row(**extra), family_live=True)
    with pytest.raises(SessionError, match="ended"):
        await rotate_session(pool, "tok", idle_s=600)  # type: ignore[arg-type]
    assert not pool.ran("SET rotated_at") and not pool.ran("INSERT")


async def test_a_single_sign_on_session_renews_with_its_identity() -> None:
    who = Identity(subject="u-1", consumer="alice", roles=("viewer",))
    pool = _session_pool(_session_row(kind="oidc", claims=json.dumps(who.claims())))
    renewed = await rotate_session(pool, "tok", idle_s=600)  # type: ignore[arg-type]
    assert renewed.identity == who


async def test_closing_a_session_returns_its_id_token_for_signing_out_at_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vault, "current", _vault)
    sealed = _vault().seal("idt", vault.session_context("fam-1"))
    pool = FakePool().on("WITH ended", {"kind": "oidc", "family": "fam-1", "id_token": sealed})
    ended = await close_session(pool, "tok")  # type: ignore[arg-type]
    assert ended is not None and (ended.kind, ended.id_token) == ("oidc", "idt")
    assert await close_session(FakePool(), "tok") is None  # type: ignore[arg-type]


async def test_an_id_token_that_does_not_open_still_ends_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vault, "current", _vault)
    other_row = _vault().seal("idt", vault.session_context("fam-2"))
    pool = FakePool().on("WITH ended", {"kind": "oidc", "family": "fam-1", "id_token": other_row})
    ended = await close_session(pool, "tok")  # type: ignore[arg-type]
    assert ended is not None and ended.id_token is None


def test_an_id_token_is_sealed_for_the_next_session_only_when_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert identity._sealed_copy(None, "fam") is None
    monkeypatch.setattr(vault, "current", lambda: vault.Vault({}))
    assert identity._sealed_copy("clear", "fam") == "clear"
    monkeypatch.setattr(vault, "current", _vault)
    sealed = identity._sealed_copy("clear", "fam")
    assert sealed is not None and vault.Vault.is_sealed(sealed)
    assert identity._sealed_copy(sealed, "fam") == sealed


# ── Throttle ─────────────────────────────────────────────────────────────


def test_refused_attempts_do_not_extend_the_wait() -> None:
    throttle = Throttle(per_min=1)
    assert throttle.admit("a", now=0)
    assert not throttle.admit("a", now=30) and not throttle.admit("a", now=59)
    assert throttle.admit("a", now=60.5)


def test_a_full_throttle_forgets_addresses_idle_for_a_minute() -> None:
    throttle = Throttle(per_min=5, max_keys=2)
    throttle.admit("a", now=0)
    throttle.admit("b", now=10)
    assert throttle.admit("c", now=65)               # "a" is forgotten to make room
    assert set(throttle._seen) == {"b", "c"}


def test_the_throttle_never_tracks_more_addresses_than_its_maximum() -> None:
    throttle = Throttle(per_min=5, max_keys=3)
    for i in range(10):
        throttle.admit(f"10.0.0.{i}", now=float(i))
    assert len(throttle._seen) <= 3  # noqa: PLR2004

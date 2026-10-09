"""Sign-in, sessions, users and grants against a database.

The identity tables are instance-wide: each test starts from none (no user,
no grant, no session, no signing key), as a first start would.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import identity
from graphos.__main__ import reset_admin_password
from graphos.app import create_app, setup_identity
from graphos.bootstrap import forget
from graphos.config import Settings
from graphos.sync import Listener

from .conftest import requires_db

FIRST = "first-password-1"
SECOND = "second-password-2"


async def _wipe(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "DELETE FROM graphos_meta.users; DELETE FROM graphos_meta.grants; "
        "DELETE FROM graphos_meta.sessions; DELETE FROM graphos_meta.signing_keys;",
    )


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    await _wipe(db_pool)
    password_file = tmp_path / "admin_password"
    password_file.write_text(FIRST + "\n")
    settings = Settings(
        db_password="placeholder", admin_password_file=password_file,
        login_max_failures=3, login_lockout_s=60,
    )
    app = create_app(settings)
    app.state.pool = db_pool
    await setup_identity(app, db_pool, settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        yield client
    await _wipe(db_pool)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(api: httpx.AsyncClient, user: str, password: str) -> dict[str, Any]:
    resp = await api.post("/v1/auth/login", json={"user": user, "password": password})
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()


async def _admin(api: httpx.AsyncClient) -> str:
    """Sign in as the bootstrap admin and do the forced change: its token."""
    first = await _login(api, "graphos", FIRST)
    resp = await api.post(
        "/v1/me/password", headers=_auth(first["access_token"]),
        json={"current": FIRST, "new": SECOND},
    )
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()["access_token"]


async def _create(api: httpx.AsyncClient, admin: str, name: str, kind: str = "person") -> str:
    resp = await api.post("/v1/users", headers=_auth(admin), json={"name": name, "kind": kind})
    assert resp.status_code == HTTPStatus.CREATED, resp.text
    return resp.json()["password"]


async def _grant(api: httpx.AsyncClient, token: str, subject: str, tenant: str, level: str):  # noqa: ANN202
    return await api.post(
        "/v1/grants", headers=_auth(token),
        json={"subject": subject, "tenant": tenant, "level": level},
    )


# ── bootstrap and the forced change ─────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_first_sign_in_must_change_the_bootstrap_password(api: httpx.AsyncClient) -> None:
    first = await _login(api, "graphos", FIRST)
    assert first["must_change"] is True and first["refresh_token"]
    token = first["access_token"]
    # Until changed: only /v1/me, /v1/me/password and logout.
    me = (await api.get("/v1/me", headers=_auth(token))).json()
    assert me["must_change"] is True and me["admin"] is True
    blocked = await api.get("/v1/users", headers=_auth(token))
    assert blocked.status_code == HTTPStatus.FORBIDDEN
    assert "change your password first" in blocked.json()["detail"]
    # Policy and the current password are checked.
    short = await api.post("/v1/me/password", headers=_auth(token),
                           json={"current": FIRST, "new": "short"})
    assert short.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    wrong = await api.post("/v1/me/password", headers=_auth(token),
                           json={"current": "not-it", "new": SECOND})
    assert wrong.status_code == HTTPStatus.FORBIDDEN
    same = await api.post("/v1/me/password", headers=_auth(token),
                          json={"current": FIRST, "new": FIRST})
    assert same.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    changed = await api.post("/v1/me/password", headers=_auth(token),
                             json={"current": FIRST, "new": SECOND})
    assert changed.status_code == HTTPStatus.OK and changed.json()["must_change"] is False
    # The old token and session are gone; the new token works.
    assert (await api.get("/v1/me", headers=_auth(token))).status_code == HTTPStatus.UNAUTHORIZED
    old = await api.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert old.status_code == HTTPStatus.UNAUTHORIZED
    fresh = changed.json()["access_token"]
    assert (await api.get("/v1/users", headers=_auth(fresh))).status_code == HTTPStatus.OK
    # The bootstrap password no longer signs in.
    again = await api.post("/v1/auth/login", json={"user": "graphos", "password": FIRST})
    assert again.status_code == HTTPStatus.UNAUTHORIZED


@requires_db
@pytest.mark.asyncio
async def test_without_a_password_file_the_first_admin_is_graphos(db_pool: asyncpg.Pool) -> None:
    await _wipe(db_pool)
    try:
        store = identity.AccessStore(db_pool)
        assert await store.bootstrap(None) == "default"
        assert await store.bootstrap(None) is None           # users exist: nothing more
        await store.load()
        user = store.user("graphos")
        assert user is not None and user.must_change and store.first_password_unchanged()
        assert store.principal("graphos").admin              # type: ignore[union-attr]
        stored = await db_pool.fetchval(
            "SELECT password_hash FROM graphos_meta.users WHERE name = 'graphos';",
        )
        assert "graphos" not in stored and identity.verify_password("graphos", stored)
        await store.set_password("graphos", "a-better-password-1", must_change=False)
        assert not store.first_password_unchanged()
    finally:
        await _wipe(db_pool)


@requires_db
@pytest.mark.asyncio
async def test_reset_admin_password_command(db_pool: asyncpg.Pool) -> None:
    await _wipe(db_pool)
    dsn = urlparse(os.environ["GRAPHOS_TEST_DSN"])
    settings = Settings(
        db_host=dsn.hostname, db_port=dsn.port or 5432, db_name=dsn.path.lstrip("/"),
        db_user=dsn.username, db_password=dsn.password,
    )
    try:
        secret = await reset_admin_password(settings)      # no admin yet: created
        store = identity.AccessStore(db_pool)
        await store.load()
        assert store.principal("graphos").admin and store.user("graphos").must_change  # type: ignore[union-attr]
        await store.set_disabled("graphos", True)
        again = await reset_admin_password(settings)       # disabled: restored
        await store.load()
        user = store.user("graphos")
        assert again != secret and user is not None and not user.disabled and user.must_change
    finally:
        await _wipe(db_pool)


# ── sign-in protection and sessions ─────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_wrong_passwords_lock_the_account_with_one_message(api: httpx.AsyncClient) -> None:
    unknown = await api.post("/v1/auth/login", json={"user": "nobody", "password": "x"})
    wrong = await api.post("/v1/auth/login", json={"user": "graphos", "password": "x"})
    assert unknown.status_code == wrong.status_code == HTTPStatus.UNAUTHORIZED
    assert unknown.json()["detail"] == wrong.json()["detail"]
    for _ in range(2):                                     # 3 failures in all: locked
        await api.post("/v1/auth/login", json={"user": "graphos", "password": "x"})
    locked = await api.post("/v1/auth/login", json={"user": "graphos", "password": FIRST})
    assert locked.status_code == HTTPStatus.UNAUTHORIZED
    assert locked.json()["detail"] == wrong.json()["detail"]


@requires_db
@pytest.mark.asyncio
async def test_refresh_rotates_and_a_reused_token_ends_the_session(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    admin = await _admin(api)
    await _create(api, admin, "ada")
    resp = await api.post("/v1/users/ada:reset-secret", headers=_auth(admin))
    ada_pw = resp.json()["password"]
    first = await _login(api, "ada", ada_pw)
    one = await api.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert one.status_code == HTTPStatus.OK
    two = one.json()
    assert two["refresh_token"] != first["refresh_token"] and two["access_token"]
    # Past the grace for a second tab: the first refresh token used again is theft,
    # and the whole session ends.
    await db_pool.execute(
        "UPDATE graphos_meta.sessions SET rotated_at = now() - interval '1 minute' "
        "WHERE rotated_at IS NOT NULL;",
    )
    reused = await api.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert reused.status_code == HTTPStatus.UNAUTHORIZED
    later = await api.post("/v1/auth/refresh", json={"refresh_token": two["refresh_token"]})
    assert later.status_code == HTTPStatus.UNAUTHORIZED


COOKIE = "graphos_session"


def _cookie_of(resp: httpx.Response) -> str:
    """The session cookie a response sets, checked for its attributes."""
    header = next(h for h in resp.headers.get_list("set-cookie") if h.startswith(f"{COOKIE}="))
    attrs = {part.strip().split("=")[0].lower() for part in header.split(";")[1:]}
    assert {"httponly", "secure", "samesite", "path", "max-age"} <= attrs, header
    assert "samesite=strict" in header.lower() and "path=/" in header.lower()
    return header.split(";")[0].split("=", 1)[1]


async def _with_cookie(
    api: httpx.AsyncClient, path: str, cookie: str, body: Any = None,
) -> httpx.Response:
    return await api.post(path, json=body or {}, headers={"Cookie": f"{COOKIE}={cookie}"})


@requires_db
@pytest.mark.asyncio
async def test_a_browser_session_lives_in_an_httponly_cookie(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    await _admin(api)
    resp = await api.post(
        "/v1/auth/login", json={"user": "graphos", "password": SECOND, "session_cookie": True},
    )
    assert resp.status_code == HTTPStatus.OK
    signed = resp.json()
    # The refresh token is in the cookie only: the page never holds it.
    assert signed["refresh_token"] is None and signed["session"] is True
    first = _cookie_of(resp)
    # A reload: no token in the page, the cookie renews the session.
    one = await _with_cookie(api, "/v1/auth/refresh", first)
    assert one.status_code == HTTPStatus.OK, one.text
    assert one.json()["refresh_token"] is None and one.json()["access_token"]
    second = _cookie_of(one)
    assert second != first
    # Another tab sends the previous cookie a moment later: an access token, no rotation.
    tab = await _with_cookie(api, "/v1/auth/refresh", first)
    assert tab.status_code == HTTPStatus.OK and tab.json()["access_token"]
    assert not any(h.startswith(f"{COOKIE}=") for h in tab.headers.get_list("set-cookie"))
    # Past that grace the old cookie is theft: the session ends.
    await db_pool.execute(
        "UPDATE graphos_meta.sessions SET rotated_at = now() - interval '1 minute' "
        "WHERE rotated_at IS NOT NULL;",
    )
    stolen = await _with_cookie(api, "/v1/auth/refresh", first)
    assert stolen.status_code == HTTPStatus.UNAUTHORIZED
    after = await _with_cookie(api, "/v1/auth/refresh", second)
    assert after.status_code == HTTPStatus.UNAUTHORIZED


@requires_db
@pytest.mark.asyncio
async def test_signing_out_clears_the_session_cookie(api: httpx.AsyncClient) -> None:
    await _admin(api)
    resp = await api.post(
        "/v1/auth/login", json={"user": "graphos", "password": SECOND, "session_cookie": True},
    )
    cookie = _cookie_of(resp)
    out = await _with_cookie(api, "/v1/auth/logout", cookie)
    assert out.status_code == HTTPStatus.OK
    cleared = next(h for h in out.headers.get_list("set-cookie") if h.startswith(f"{COOKIE}="))
    assert "max-age=0" in cleared.lower()
    after = await _with_cookie(api, "/v1/auth/refresh", cookie)
    assert after.status_code == HTTPStatus.UNAUTHORIZED
    # Without a cookie or a token there is nothing to renew.
    assert (await api.post("/v1/auth/refresh", json={})).status_code == HTTPStatus.UNAUTHORIZED


@requires_db
@pytest.mark.asyncio
async def test_logout_ends_the_session(api: httpx.AsyncClient) -> None:
    admin = await _admin(api)
    password = await _create(api, admin, "sam", kind="service")
    service = await _login(api, "sam", password)
    assert service["refresh_token"] is None and service["must_change"] is False
    first = await _login(api, "graphos", SECOND)
    out = await api.post("/v1/auth/logout", json={"refresh_token": first["refresh_token"]})
    assert out.status_code == HTTPStatus.OK and out.json() == {"end_session_url": None}
    after = await api.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert after.status_code == HTTPStatus.UNAUTHORIZED


@requires_db
@pytest.mark.asyncio
async def test_disabling_or_resetting_a_user_stops_its_tokens_at_once(
    api: httpx.AsyncClient,
) -> None:
    admin = await _admin(api)
    password = await _create(api, admin, "svc", kind="service")
    token = (await _login(api, "svc", password))["access_token"]
    assert (await api.get("/v1/me", headers=_auth(token))).status_code == HTTPStatus.OK
    reset = await api.post("/v1/users/svc:reset-secret", headers=_auth(admin))
    assert reset.json()["user"]["must_change"] is False          # a service keeps its secret
    assert (await api.get("/v1/me", headers=_auth(token))).status_code == HTTPStatus.UNAUTHORIZED
    token = (await _login(api, "svc", reset.json()["password"]))["access_token"]
    resp = await api.patch("/v1/users/svc", headers=_auth(admin), json={"disabled": True})
    assert resp.status_code == HTTPStatus.OK and resp.json()["disabled"] is True
    assert (await api.get("/v1/me", headers=_auth(token))).status_code == HTTPStatus.UNAUTHORIZED
    refused = await api.post(
        "/v1/auth/login", json={"user": "svc", "password": reset.json()["password"]},
    )
    assert refused.status_code == HTTPStatus.UNAUTHORIZED


# ── grants, ownership and the last admin ────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_a_service_account_creates_tenants_and_owns_them(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    tenant = f"sol_{uuid4().hex[:8]}"
    admin = await _admin(api)
    password = await _create(api, admin, "catalogue-loader", kind="service")
    resp = await _grant(api, admin, "user:catalogue-loader", "sol_*", "create_tenants")
    assert resp.status_code == HTTPStatus.CREATED, resp.text
    svc = (await _login(api, "catalogue-loader", password))["access_token"]
    try:
        made = await api.post("/v1/tenants", headers=_auth(svc), json={
            "name": tenant, "vertex_labels": ["Doc"], "edge_labels": ["LINKS"],
        })
        assert made.status_code == HTTPStatus.OK, made.text
        me = (await api.get("/v1/me", headers=_auth(svc))).json()
        assert me["levels"] == {tenant: "owner"} and me["create_tenants"] == ["sol_*"]
        # It publishes schema on its tenant and writes to it, without being admin.
        more = await api.post("/v1/tenants", headers=_auth(svc), json={
            "name": tenant, "vertex_labels": ["Doc", "Tag"],
        })
        assert more.status_code == HTTPStatus.OK, more.text
        nodes = await api.post(f"/v1/{tenant}/graph/nodes", headers=_auth(svc), json={
            "nodes": [{"id": "d1", "label": "Doc", "properties": {}}],
        })
        assert nodes.status_code == HTTPStatus.OK, nodes.text
        outside = await api.post("/v1/tenants", headers=_auth(svc), json={"name": "acme_x"})
        assert outside.status_code == HTTPStatus.FORBIDDEN
        assert (await api.get("/v1/users", headers=_auth(svc))).status_code == HTTPStatus.FORBIDDEN
        gone = await api.delete(f"/v1/tenants/{tenant}", headers=_auth(svc))
        assert gone.status_code == HTTPStatus.OK, gone.text
        me = (await api.get("/v1/me", headers=_auth(svc))).json()
        assert me["levels"] == {}                          # its grant left with the tenant
    finally:
        async with db_pool.acquire() as conn:
            if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", tenant):
                await conn.execute(f"SELECT drop_graph('{tenant}', true);")
            await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", tenant)
            await conn.execute("DELETE FROM graphos_meta.changes WHERE tenant = $1;", tenant)
        forget(tenant)


@requires_db
@pytest.mark.asyncio
async def test_an_owner_manages_access_to_its_tenant_only(api: httpx.AsyncClient) -> None:
    admin = await _admin(api)
    owner_pw = await _create(api, admin, "olive")
    await _create(api, admin, "pat")
    made = await _grant(api, admin, "user:olive", "acme", "owner")
    assert made.status_code == HTTPStatus.CREATED
    first = (await _login(api, "olive", owner_pw))["access_token"]
    changed = await api.post("/v1/me/password", headers=_auth(first),
                             json={"current": owner_pw, "new": "olive-password-1"})
    olive = changed.json()["access_token"]
    # On acme, any level, owner included.
    for level in ("read", "owner"):
        resp = await _grant(api, olive, "user:pat", "acme", level)
        assert resp.status_code == HTTPStatus.CREATED, resp.text
    assert (await _grant(api, olive, "user:pat", "acme", "read")).status_code == HTTPStatus.OK
    # Not elsewhere, not patterns, not global rights, not unknown users.
    refused = (("zeta", "read"), ("ac*", "read"), ("*", "admin"), ("acme", "create_tenants"))
    for tenant, level in refused:
        resp = await _grant(api, olive, "user:pat", tenant, level)
        assert resp.status_code == HTTPStatus.FORBIDDEN, (tenant, level, resp.text)
    unknown = await _grant(api, olive, "user:nobody", "acme", "read")
    assert unknown.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    # It sees the grants on acme and its own, not the admin's.
    listed = (await api.get("/v1/grants", headers=_auth(olive))).json()["grants"]
    assert {(g["subject"], g["tenant"], g["level"]) for g in listed} == {
        ("user:olive", "acme", "owner"),
        ("user:pat", "acme", "read"),
        ("user:pat", "acme", "owner"),
    }
    revoked = await api.delete("/v1/grants", headers=_auth(olive),
                               params={"subject": "user:pat", "tenant": "acme", "level": "owner"})
    assert revoked.status_code == HTTPStatus.NO_CONTENT


@requires_db
@pytest.mark.asyncio
async def test_the_last_admin_cannot_be_removed(api: httpx.AsyncClient) -> None:
    admin = await _admin(api)
    only = await api.delete("/v1/grants", headers=_auth(admin),
                            params={"subject": "user:graphos", "tenant": "*", "level": "admin"})
    assert only.status_code == HTTPStatus.CONFLICT
    last = await api.delete("/v1/users/graphos", headers=_auth(admin))
    assert last.status_code == HTTPStatus.CONFLICT
    await _create(api, admin, "ops")
    assert (await _grant(api, admin, "user:ops", "*", "admin")).status_code == HTTPStatus.CREATED
    bad = await _grant(api, admin, "user:ops", "acme", "admin")
    assert bad.status_code == HTTPStatus.UNPROCESSABLE_ENTITY      # admin is on "*"
    two = await api.delete("/v1/grants", headers=_auth(admin),
                           params={"subject": "user:ops", "tenant": "*", "level": "admin"})
    assert two.status_code == HTTPStatus.NO_CONTENT


# ── replicas ────────────────────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_a_change_on_one_replica_reaches_another(db_pool: asyncpg.Pool) -> None:
    await _wipe(db_pool)
    one, two = identity.AccessStore(db_pool), identity.AccessStore(db_pool)
    listener = Listener(db_pool)
    listener.subscribe(identity.ACCESS_CHANNEL, two.on_notify)
    listener.start()
    try:
        await asyncio.sleep(0.5)                            # listening
        await one.create_user("ada", "person", by="test")
        await one.grant("user:ada", "acme", "write", by="test")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            principal = two.principal("ada")
            if principal is not None and principal.level("acme") is identity.Level.WRITE:
                break
            await asyncio.sleep(0.05)
        assert two.principal("ada").level("acme") is identity.Level.WRITE  # type: ignore[union-attr]
    finally:
        await listener.stop()
        await _wipe(db_pool)

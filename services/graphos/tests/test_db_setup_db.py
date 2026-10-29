"""The database set-up at start (graphos.db_setup, ADR-0012), on a real database.

DB-gated. Two cases:

- a fresh, empty database and a role that does not exist, with no db-init
  folder: a graphos process started with the admin settings sets them up, the
  role gets what deploy/db-init/ gives ``graphos_app``, and the API then
  serves: sign-in, a tenant, nodes and an edge;
- the test stack's own database, set up by the db-init folder as 0.14.5 was:
  graphos processes started on it with and without the admin settings, and the
  admin pass itself, change nothing: the role, its password, its grants and
  the extensions are the same before and after.

The database test stack (scripts/run-db-tests.sh) passes the app role's
password as ``GRAPHOS_TEST_APP_PASSWORD``; without it the second case skips.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import httpx
import pytest

from graphos import db_setup

from .conftest import requires_db

SERVICE = Path(__file__).resolve().parents[1]
APP_ROLE = "graphos_app"

# A role's privileges as the set-up and db-init give them, without names or
# oids that differ between databases.
_PROFILE_SQL = """
WITH r AS (SELECT oid FROM pg_roles WHERE rolname = $1)
SELECT json_build_object(
  'attributes', (SELECT json_build_object(
      'super', rolsuper, 'inherit', rolinherit, 'createrole', rolcreaterole,
      'createdb', rolcreatedb, 'login', rolcanlogin, 'replication', rolreplication,
      'bypassrls', rolbypassrls, 'connlimit', rolconnlimit, 'config', rolconfig)
    FROM pg_roles WHERE rolname = $1),
  'member_of', (SELECT coalesce(json_agg(m.roleid::regrole::text ORDER BY 1), '[]')
    FROM pg_auth_members m, r WHERE m.member = r.oid),
  'database', (SELECT coalesce(json_agg(a.privilege_type ORDER BY 1), '[]')
    FROM pg_database d, aclexplode(d.datacl) a, r
    WHERE d.datname = current_database() AND a.grantee = r.oid),
  'ag_catalog', (SELECT coalesce(json_agg(a.privilege_type ORDER BY 1), '[]')
    FROM pg_namespace n, aclexplode(n.nspacl) a, r
    WHERE n.nspname = 'ag_catalog' AND a.grantee = r.oid),
  'relations', (SELECT json_object_agg(c.relname, (
      SELECT coalesce(json_agg(a.privilege_type ORDER BY 1), '[]')
        FROM aclexplode(c.relacl) a, r WHERE a.grantee = r.oid))
    FROM pg_class c WHERE c.relnamespace = 'ag_catalog'::regnamespace
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')),
  'parameters', (SELECT coalesce(json_agg(p.parname ORDER BY 1), '[]')
    FROM pg_parameter_acl p, aclexplode(p.paracl) a, r WHERE a.grantee = r.oid),
  'extensions', (SELECT json_agg(extname ORDER BY 1) FROM pg_extension));
"""

# Everything an upgrade must not change, with grantors and the password hash.
_SNAPSHOT_SQL = """
SELECT json_build_object(
  'roles', (SELECT json_agg(json_build_object(
      'name', rolname, 'super', rolsuper, 'inherit', rolinherit,
      'createrole', rolcreaterole, 'createdb', rolcreatedb, 'login', rolcanlogin,
      'replication', rolreplication, 'bypassrls', rolbypassrls,
      'connlimit', rolconnlimit, 'password', rolpassword, 'validuntil', rolvaliduntil)
      ORDER BY rolname)
    FROM pg_authid WHERE rolname NOT LIKE 'pg\\_%'),
  'settings', (SELECT json_agg(json_build_object(
      'role', setrole::regrole::text, 'db', setdatabase, 'config', setconfig)
      ORDER BY setrole::regrole::text, setdatabase)
    FROM pg_db_role_setting),
  'members', (SELECT json_agg(json_build_object(
      'role', roleid::regrole::text, 'member', member::regrole::text,
      'grantor', grantor::regrole::text, 'admin', admin_option,
      'inherit', inherit_option, 'set', set_option) ORDER BY 1::text)
    FROM pg_auth_members),
  'database', (SELECT datacl::text FROM pg_database WHERE datname = current_database()),
  'owner', (SELECT datdba::regrole::text FROM pg_database WHERE datname = current_database()),
  'schemas', (SELECT json_object_agg(nspname, json_build_object(
      'owner', nspowner::regrole::text, 'acl', nspacl::text))
    FROM pg_namespace WHERE nspname IN ('ag_catalog', 'graphos_meta', 'public')),
  'ag_catalog', (SELECT json_object_agg(relname, json_build_object(
      'owner', relowner::regrole::text, 'acl', relacl::text))
    FROM pg_class WHERE relnamespace = 'ag_catalog'::regnamespace
      AND relkind IN ('r', 'p', 'v', 'm', 'f', 'S')),
  'graphos_meta', (SELECT json_object_agg(relname, relowner::regrole::text)
    FROM pg_class WHERE relnamespace = to_regnamespace('graphos_meta')
      AND relkind IN ('r', 'S')),
  'parameters', (SELECT json_agg(json_build_object('name', parname, 'acl', paracl::text)
      ORDER BY parname) FROM pg_parameter_acl),
  'extensions', (SELECT json_agg(json_build_object(
      'name', extname, 'version', extversion, 'owner', extowner::regrole::text,
      'schema', extnamespace::regnamespace::text) ORDER BY extname)
    FROM pg_extension));
"""


def _dsn(database: str | None = None) -> str:
    parts = urlsplit(os.environ["GRAPHOS_TEST_DSN"])
    if database is None:
        return parts.geturl()
    return urlunsplit(parts._replace(path=f"/{database}"))


async def _json(dsn: str, sql: str, *args: Any) -> Any:
    conn = await asyncpg.connect(dsn)
    try:
        return json.loads(await conn.fetchval(sql, *args))
    finally:
        await conn.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def _graphos(env: dict[str, str]) -> Iterator[httpx.Client]:
    """A graphos-api process (``python -m graphos``) until it is ready."""
    port = _free_port()
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "graphos"], cwd=SERVICE, stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ, "GRAPHOS_BIND_HOST": "127.0.0.1", "GRAPHOS_BIND_PORT": str(port),
                 "GRAPHOS_LOG_JSON": "false", "GRAPHOS_REGISTRY_SYNC_S": "3600", **env},
        )
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60)

        def output() -> str:
            log.seek(0)
            return log.read().decode("utf-8", "replace")[-4000:]

        try:
            deadline = time.monotonic() + 90
            while True:
                assert proc.poll() is None, f"graphos exited during start:\n{output()}"
                try:
                    if client.get("/readyz").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, f"graphos did not become ready:\n{output()}"
                time.sleep(0.25)
            yield client
        finally:
            client.close()
            proc.terminate()
            proc.wait(timeout=30)


def _db_env(database: str, user: str, password: str, *, admin: bool) -> dict[str, str]:
    parts = urlsplit(os.environ["GRAPHOS_TEST_DSN"])
    env = {
        "GRAPHOS_DB_HOST": parts.hostname or "127.0.0.1",
        "GRAPHOS_DB_PORT": str(parts.port or 5432),
        "GRAPHOS_DB_NAME": database,
        "GRAPHOS_DB_USER": user,
        "GRAPHOS_DB_PASSWORD": password,
        # A process environment would otherwise pass on the test's own.
        "GRAPHOS_DB_ADMIN_USER": "",
        "GRAPHOS_DB_ADMIN_PASSWORD": "",
        # The stack's graphos-api sealed its signing keys with this key; a
        # process on the same database cannot start without it.
        "GRAPHOS_MASTER_KEY_FILE": os.environ.get("GRAPHOS_TEST_MASTER_KEY_FILE", ""),
    }
    if admin:
        env["GRAPHOS_DB_ADMIN_USER"] = parts.username or "graphos"
        env["GRAPHOS_DB_ADMIN_PASSWORD"] = parts.password or ""
    return env


# ── A fresh database, no db-init folder ─────────────────────────────────────


async def _create_database(name: str) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(f"CREATE DATABASE {db_setup.quote_ident(name)};")
    finally:
        await conn.close()


async def _drop(database: str, role: str | None = None) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        name = db_setup.quote_ident(database)
        await conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE);")
        if role and await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1;", role):
            # Grants on shared objects outlive the database.
            await conn.execute(
                f"REVOKE SET ON PARAMETER temp_file_limit FROM {db_setup.quote_ident(role)};")
            await conn.execute(f"DROP ROLE {db_setup.quote_ident(role)};")
    finally:
        await conn.close()


@requires_db
def test_a_fresh_database_is_set_up_by_graphos_api_and_then_serves() -> None:
    suffix = uuid4().hex[:10]
    database, role, password = f"setup_fresh_{suffix}", f"setup_app_{suffix}", uuid4().hex
    asyncio.run(_create_database(database))
    try:
        assert asyncio.run(_json(
            _dsn(database), "SELECT to_json(count(*)) FROM pg_roles WHERE rolname = $1;", role,
        )) == 0
        with _graphos(_db_env(database, role, password, admin=True)) as api:
            assert api.get("/readyz").json()["status"] == "ready"
            first = api.post("/v1/auth/login", json={"user": "graphos", "password": "graphos"})
            assert first.status_code == 200, first.text
            changed = api.post(
                "/v1/me/password", json={"current": "graphos", "new": f"fresh-{uuid4().hex}"},
                headers={"Authorization": f"Bearer {first.json()['access_token']}"},
            )
            assert changed.status_code == 200, changed.text
            auth = {"Authorization": f"Bearer {changed.json()['access_token']}"}
            tenant = f"acme_{suffix}"
            for path, body in [
                ("/v1/tenants", {"name": tenant, "vertex_labels": ["Doc"],
                                 "edge_labels": ["LINKS_TO"]}),
                (f"/v1/{tenant}/graph/nodes", {"nodes": [{"id": "a", "label": "Doc"},
                                                       {"id": "b", "label": "Doc"}]}),
                (f"/v1/{tenant}/graph/edges", {"edges": [
                    {"src_id": "a", "src_label": "Doc", "dst_id": "b", "dst_label": "Doc",
                     "type": "LINKS_TO"}]}),
            ]:
                written = api.post(path, json=body, headers=auth)
                assert written.status_code < 300, (path, written.text)
            stats = api.get(f"/v1/{tenant}/graph/stats", headers=auth)
            assert stats.status_code == 200, stats.text
            assert "2" in json.dumps(stats.json()) and "1" in json.dumps(stats.json())

        # The role has what deploy/db-init/ gives graphos_app, no more, no less.
        fresh = asyncio.run(_json(_dsn(database), _PROFILE_SQL, role))
        reference = asyncio.run(_json(_dsn(), _PROFILE_SQL, APP_ROLE))
        assert fresh == reference
        # It signs in with the password graphos was given (SCRAM, never sent in clear).
        stored = asyncio.run(_json(
            _dsn(), "SELECT to_json(rolpassword) FROM pg_authid WHERE rolname = $1;", role))
        assert stored.startswith("SCRAM-SHA-256$") and password not in stored
        # A second start finds nothing to do.
        before = asyncio.run(_json(_dsn(database), _SNAPSHOT_SQL))
        with _graphos(_db_env(database, role, password, admin=True)):
            pass
        assert asyncio.run(_json(_dsn(database), _SNAPSHOT_SQL)) == before
    finally:
        asyncio.run(_drop(database, role))


# ── The database the db-init folder set up (as 0.14.5) ──────────────────────


@pytest.fixture
def app_password() -> str:
    password = os.environ.get("GRAPHOS_TEST_APP_PASSWORD")
    if not password:
        pytest.skip("GRAPHOS_TEST_APP_PASSWORD is not set (scripts/run-db-tests.sh sets it)")
    return password


@requires_db
def test_a_database_set_up_by_db_init_starts_with_nothing_changed(app_password: str) -> None:
    database = urlsplit(_dsn()).path.lstrip("/")
    before = asyncio.run(_json(_dsn(), _SNAPSHOT_SQL))
    for admin in (False, True):
        with _graphos(_db_env(database, APP_ROLE, app_password, admin=admin)) as api:
            assert api.get("/readyz").status_code == 200
            signed_in = api.post("/v1/auth/login", json={"user": "nobody", "password": "x"})
            assert signed_in.status_code == 401
        assert asyncio.run(_json(_dsn(), _SNAPSHOT_SQL)) == before, f"admin={admin}"


@requires_db
def test_the_admin_pass_itself_changes_nothing_on_a_database_set_up_by_db_init(
    app_password: str,
) -> None:
    database = urlsplit(_dsn()).path.lstrip("/")
    before = asyncio.run(_json(_dsn(), _SNAPSHOT_SQL))

    async def admin_pass() -> list[str]:
        conn = await asyncpg.connect(_dsn())
        try:
            assert await db_setup.inspect(conn, APP_ROLE) == db_setup.Missing()
            return await db_setup.apply(conn, APP_ROLE, database, app_password)
        finally:
            await conn.close()

    assert asyncio.run(admin_pass()) == []
    assert asyncio.run(_json(_dsn(), _SNAPSHOT_SQL)) == before


@requires_db
def test_graphos_role_sees_nothing_missing_on_a_database_set_up_by_db_init(
    app_password: str,
) -> None:
    parts = urlsplit(_dsn())
    app_dsn = urlunsplit(parts._replace(
        netloc=f"{APP_ROLE}:{app_password}@{parts.hostname}:{parts.port or 5432}"))

    async def look() -> db_setup.Missing:
        conn = await asyncpg.connect(app_dsn)
        try:
            return await db_setup.inspect(conn, APP_ROLE)
        finally:
            await conn.close()

    assert asyncio.run(look()) == db_setup.Missing()


# ── A restored database: only the missing grant ─────────────────────────────


@requires_db
def test_a_restored_database_gets_only_its_database_grant(app_password: str) -> None:
    suffix = uuid4().hex[:10]
    database = f"setup_restored_{suffix}"
    asyncio.run(_create_database(database))

    async def scenario() -> tuple[list[str], db_setup.Missing]:
        conn = await asyncpg.connect(_dsn(database))
        try:
            # What a restore of the dump leaves: everything but the database grant.
            for e in db_setup.EXTENSIONS:
                await conn.execute(f"CREATE EXTENSION {db_setup.quote_ident(e)};")
            await conn.execute(f"GRANT USAGE ON SCHEMA ag_catalog TO {APP_ROLE};")
            await conn.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ag_catalog "
                f"TO {APP_ROLE};")
            await conn.execute(
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ag_catalog TO {APP_ROLE};")
            done = await db_setup.apply(conn, APP_ROLE, database, app_password)
            return done, await db_setup.inspect(conn, APP_ROLE)
        finally:
            await conn.close()

    try:
        done, after = asyncio.run(scenario())
        assert done == ["CONNECT on the database", "CREATE on the database"]
        assert after == db_setup.Missing()
    finally:
        asyncio.run(_drop(database))

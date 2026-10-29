"""The database set-up at start (graphos.db_setup, ADR-0012), on a fake database.

The fake answers the set-up's inspection queries from its state and applies
the statements the set-up sends, so the tests see what was done and what was
left alone. The database itself is checked in test_db_setup_db.py.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from graphos import db_setup
from graphos.config import Settings

APP_PW = "app-Secret-1"
ADMIN_PW = "admin-Secret-2"
ALL_DB = {"CONNECT", "CREATE"}
ALL_TABLE = set(db_setup.TABLE_PRIVILEGES)
ALL_SEQ = set(db_setup.SEQUENCE_PRIVILEGES)


@dataclass
class FakeDb:
    """A database's catalogue, as far as the set-up reads and writes it."""

    extensions: set[str] = field(default_factory=set)
    roles: dict[str, tuple[int, bool]] = field(default_factory=dict)
    passwords: dict[str, str] = field(default_factory=dict)
    database: dict[int, set[str]] = field(default_factory=dict)
    schema: set[int] = field(default_factory=set)
    tables: dict[str, dict[int, set[str]]] = field(default_factory=dict)
    sequences: dict[str, dict[int, set[str]]] = field(default_factory=dict)
    stats: set[int] = field(default_factory=set)
    parameters: dict[str, set[int]] = field(default_factory=dict)
    executed: list[str] = field(default_factory=list)
    fail_on: str | None = None

    def create_age(self) -> None:
        self.extensions.add("age")
        self.tables = {"ag_graph": {}, "ag_label": {}}
        self.sequences = {"ag_label_id_seq": {}}

    def role(self, name: str, password: str, *, superuser: bool = False) -> int:
        oid = 1000 + len(self.roles)
        self.roles[name] = (oid, superuser)
        self.passwords[name] = password
        return oid

    def grant_all(self, oid: int) -> None:
        """What deploy/db-init/01-app-role.sh gives."""
        self.database[oid] = set(ALL_DB)
        self.schema.add(oid)
        for grants in self.tables.values():
            grants[oid] = set(ALL_TABLE)
        for grants in self.sequences.values():
            grants[oid] = set(ALL_SEQ)
        self.stats.add(oid)
        self.parameters["temp_file_limit"] = {oid}

    def snapshot(self) -> Any:
        return repr((sorted(self.extensions), sorted(self.roles.items()),
                     sorted(self.passwords.items()), sorted(self.database.items()),
                     sorted(self.schema), sorted(self.tables.items()),
                     sorted(self.sequences.items()), sorted(self.stats),
                     sorted(self.parameters.items())))


class FakeConn:
    """The asyncpg.Connection methods the set-up uses, on a :class:`FakeDb`."""

    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self.closed = False

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        saved = copy.deepcopy(vars(self.db))
        try:
            yield
        except Exception:
            executed = self.db.executed
            vars(self.db).update(saved)
            self.db.executed = executed
            raise

    async def close(self) -> None:
        self.closed = True

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        db = self.db
        if sql == db_setup._EXTENSIONS_SQL:
            return [{"extname": e} for e in args[0] if e in db.extensions]
        if sql == db_setup._DATABASE_SQL:
            have = db.database.get(args[0], set())
            return [{"priv": p} for p in sorted(args[1]) if p not in have]
        if sql == db_setup._RELATIONS_SQL:
            oid, kinds, _default, privs = args
            relations = db.sequences if kinds == ["S"] else db.tables
            return [{"relname": name} for name, grants in sorted(relations.items())
                    if not set(privs) <= grants.get(oid, set())]
        if sql == db_setup._PARAMETERS_SQL:
            oid, names = args
            return [{"name": p} for p in sorted(names) if oid not in db.parameters.get(p, set())]
        raise AssertionError(f"unexpected fetch: {sql}")

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        db = self.db
        if sql == db_setup._ROLE_SQL:
            found = db.roles.get(args[0])
            return None if found is None else {"oid": found[0], "rolsuper": found[1]}
        if sql == db_setup._SCHEMA_SQL:
            return (args[0] in db.schema, "age" in db.extensions)
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if sql == db_setup._STATS_SQL:
            return args[0] in self.db.stats
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        db = self.db
        db.executed.append(sql)
        if db.fail_on and db.fail_on in sql:
            raise asyncpg.InsufficientPrivilegeError("permission denied (fake)")
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            return "SELECT 1"
        if m := re.fullmatch(r'CREATE EXTENSION IF NOT EXISTS "(\w+)"( SCHEMA "public")?;', sql):
            if m[1] == "age":
                db.create_age()
            db.extensions.add(m[1])
        elif m := re.fullmatch(r'CREATE ROLE "(\w+)" .* PASSWORD \'(.+)\';', sql):
            db.role(m[1], m[2])
        elif sql.startswith("ALTER ROLE"):
            pass
        elif m := re.fullmatch(r'GRANT ([A-Z, ]+) ON DATABASE "\w+" TO "(\w+)";', sql):
            db.database.setdefault(db.roles[m[2]][0], set()).update(m[1].split(", "))
        elif m := re.fullmatch(r'GRANT USAGE ON SCHEMA ag_catalog TO "(\w+)";', sql):
            db.schema.add(db.roles[m[1]][0])
        elif m := re.fullmatch(
            r'GRANT ([A-Z, ]+) ON (SEQUENCE )?ag_catalog\."(\w+)" TO "(\w+)";', sql,
        ):
            relations = db.sequences if m[2] else db.tables
            relations[m[3]].setdefault(db.roles[m[4]][0], set()).update(m[1].split(", "))
        elif m := re.fullmatch(r'GRANT "pg_read_all_stats" TO "(\w+)";', sql):
            db.stats.add(db.roles[m[1]][0])
        elif m := re.fullmatch(r'GRANT SET ON PARAMETER (\w+) TO "(\w+)";', sql):
            db.parameters.setdefault(m[1], set()).add(db.roles[m[2]][0])
        else:
            raise AssertionError(f"unexpected statement: {sql}")
        return "OK"


def _settings(tmp_path: Path, *, admin: bool = True, admin_pw: str | None = ADMIN_PW) -> Settings:
    kwargs: dict[str, Any] = {"db_user": "graphos_app", "db_password": APP_PW, "db_name": "graphos"}
    if admin:
        kwargs["db_admin_user"] = "graphos"
        pw_file = tmp_path / "graphos_pw.txt"
        if admin_pw is not None:
            pw_file.write_text(admin_pw + "\n", encoding="utf-8")
        kwargs["db_admin_password_file"] = pw_file
    return Settings(**kwargs)


@pytest.fixture
def connect(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Route the set-up's sign-ins to a FakeDb, checking each role's password."""

    class Connector:
        db = FakeDb()
        signed_in: list[str] = []

        async def __call__(self, dsn: str) -> FakeConn:
            user, password = re.match(r"postgresql://([^:]+):([^@]+)@", dsn).groups()  # type: ignore[union-attr]
            self.signed_in.append(user)
            if self.db.passwords.get(user) != password:
                raise asyncpg.InvalidPasswordError(
                    f'password authentication failed for user "{user}"')
            return FakeConn(self.db)

    connector = Connector()
    connector.db.role("graphos", ADMIN_PW, superuser=True)
    monkeypatch.setattr(db_setup, "_connect", connector)
    return connector


def _set_up_by_db_init(db: FakeDb) -> int:
    """A database as 0.14.5's db-init folder leaves it."""
    db.create_age()
    db.extensions |= {"pg_trgm", "pg_stat_statements"}
    oid = db.role("graphos_app", APP_PW)
    db.grant_all(oid)
    return oid


# ── Without the admin settings: as before ───────────────────────────────────


async def test_without_an_admin_role_nothing_is_looked_at(tmp_path: Path, connect: Any) -> None:
    report = await db_setup.ensure_database_setup(_settings(tmp_path, admin=False))
    assert report == db_setup.Report(configured=False)
    assert connect.signed_in == []


def test_an_empty_admin_user_is_unset() -> None:
    settings = Settings(db_password="x", db_admin_user="  ", db_admin_password_file="")
    assert settings.db_admin_user is None
    assert settings.db_admin_password_file is None


# ── A database set up already (0.14.5, db-init mounted) ─────────────────────


async def test_a_database_set_up_by_db_init_is_left_as_it_is(tmp_path: Path, connect: Any) -> None:
    _set_up_by_db_init(connect.db)
    before = connect.db.snapshot()
    report = await db_setup.ensure_database_setup(_settings(tmp_path))
    assert report.admin_used is False and report.done == [] and report.error is None
    assert connect.signed_in == ["graphos_app"]          # never as the admin
    assert connect.db.executed == []
    assert connect.db.snapshot() == before


async def test_a_stale_or_missing_admin_password_file_does_no_harm_when_nothing_is_missing(
    tmp_path: Path, connect: Any,
) -> None:
    _set_up_by_db_init(connect.db)
    report = await db_setup.ensure_database_setup(_settings(tmp_path, admin_pw=None))
    assert report.error is None and report.admin_used is False


async def test_the_admin_pass_on_a_database_set_up_by_db_init_does_nothing(connect: Any) -> None:
    _set_up_by_db_init(connect.db)
    before = connect.db.snapshot()
    done = await db_setup.apply(FakeConn(connect.db), "graphos_app", "graphos", APP_PW)
    assert done == []
    assert connect.db.executed == ["SELECT pg_advisory_xact_lock($1);"]
    assert connect.db.snapshot() == before


# ── A fresh database, no db-init folder ─────────────────────────────────────


async def test_a_fresh_database_gets_what_db_init_gives(
    tmp_path: Path, connect: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    report = await db_setup.ensure_database_setup(_settings(tmp_path))
    assert report.admin_used and report.error is None
    assert connect.signed_in == ["graphos_app", "graphos"]
    db = connect.db
    assert db.extensions == {"age", "pg_trgm", "pg_stat_statements"}
    oid = db.roles["graphos_app"][0]
    assert db.roles["graphos_app"] == (oid, False)
    reference = FakeDb()
    reference.create_age()
    reference.grant_all(oid)
    assert (db.database, db.schema, db.tables, db.sequences, db.stats, db.parameters) == (
        reference.database, reference.schema, reference.tables, reference.sequences,
        reference.stats, reference.parameters)
    assert "role" in report.done and "extension age" in report.done
    # The role signs in with the password graphos has; nothing carried it in clear.
    assert db.passwords["graphos_app"].startswith("SCRAM-SHA-256$4096:")
    assert not any(APP_PW in s or ADMIN_PW in s for s in db.executed)
    assert APP_PW not in caplog.text and ADMIN_PW not in caplog.text
    create = next(s for s in db.executed if s.startswith("CREATE ROLE"))
    assert "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION" in create
    assert any(s.startswith('ALTER ROLE "graphos_app" SET search_path') for s in db.executed)


async def test_extensions_come_before_the_grants_on_their_tables(connect: Any) -> None:
    await db_setup.apply(FakeConn(connect.db), "graphos_app", "graphos", APP_PW)
    executed = connect.db.executed
    age = executed.index('CREATE EXTENSION IF NOT EXISTS "age";')
    assert all(age < executed.index(s) for s in executed if "ag_catalog." in s)
    # The others in public, whatever the admin's search_path (db-init's starts with ag_catalog).
    assert 'CREATE EXTENSION IF NOT EXISTS "pg_trgm" SCHEMA "public";' in executed
    assert 'CREATE EXTENSION IF NOT EXISTS "pg_stat_statements" SCHEMA "public";' in executed


async def test_a_second_run_does_nothing(connect: Any) -> None:
    await db_setup.apply(FakeConn(connect.db), "graphos_app", "graphos", APP_PW)
    before = connect.db.snapshot()
    connect.db.executed.clear()
    assert await db_setup.apply(FakeConn(connect.db), "graphos_app", "graphos", APP_PW) == []
    assert connect.db.snapshot() == before


# ── Only what is missing ────────────────────────────────────────────────────


async def test_only_the_missing_grant_is_given(tmp_path: Path, connect: Any) -> None:
    # A database restored from a dump: the database-level grant is not in it.
    oid = _set_up_by_db_init(connect.db)
    connect.db.database[oid] = {"CONNECT"}
    report = await db_setup.ensure_database_setup(_settings(tmp_path))
    assert report.done == ["CREATE on the database"]
    assert connect.db.executed[1:] == ['GRANT CREATE ON DATABASE "graphos" TO "graphos_app";']


async def test_an_existing_role_is_never_altered(
    tmp_path: Path, connect: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    _set_up_by_db_init(connect.db)
    connect.db.passwords["graphos_app"] = "the-password-in-the-database"
    before = connect.db.snapshot()
    report = await db_setup.ensure_database_setup(_settings(tmp_path))
    assert report.admin_used and report.done == []
    assert connect.db.snapshot() == before
    assert not any(s.startswith(("ALTER ROLE", "CREATE ROLE")) for s in connect.db.executed)
    assert "never changes an existing role's password" in caplog.text


async def test_a_superuser_app_role_gets_only_the_extensions(tmp_path: Path, connect: Any) -> None:
    settings = _settings(tmp_path).model_copy(
        update={"db_user": "graphos", "db_password": None,
                "db_password_file": tmp_path / "graphos_pw.txt"})
    report = await db_setup.ensure_database_setup(settings)
    assert report.done == ["extension age", "extension pg_trgm", "extension pg_stat_statements"]
    assert not any(s.startswith(("GRANT", "CREATE ROLE")) for s in connect.db.executed)


# ── Failures never stop a start ─────────────────────────────────────────────


async def test_a_wrong_admin_password_is_logged_and_start_goes_on(
    tmp_path: Path, connect: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    report = await db_setup.ensure_database_setup(_settings(tmp_path, admin_pw="wrong"))
    assert report.admin_used and report.error and "InvalidPasswordError" in report.error
    assert "could not sign in as the admin role" in caplog.text
    assert connect.db.executed == []


async def test_an_unreadable_admin_password_file_is_logged(
    tmp_path: Path, connect: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    report = await db_setup.ensure_database_setup(_settings(tmp_path, admin_pw=None))
    assert report.error and "GRAPHOS_DB_ADMIN_PASSWORD_FILE" in report.error
    assert "could not sign in as the admin role" in caplog.text


async def test_a_failed_statement_rolls_everything_back(
    tmp_path: Path, connect: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    connect.db.fail_on = "pg_read_all_stats"
    before = connect.db.snapshot()
    report = await db_setup.ensure_database_setup(_settings(tmp_path))
    assert report.error and "InsufficientPrivilegeError" in report.error
    assert connect.db.snapshot() == before
    assert "rolled back" in caplog.text


# ── Statements ──────────────────────────────────────────────────────────────


def test_names_are_quoted() -> None:
    sql = db_setup.statements(
        db_setup.Missing(database=["CREATE"], stats=True), 'odd"role', 'db"x', None)
    assert sql == ['GRANT CREATE ON DATABASE "db""x" TO "odd""role";',
                   'GRANT "pg_read_all_stats" TO "odd""role";']
    assert db_setup.quote_literal("a'b") == "'a''b'"


def test_a_new_role_without_its_verifier_is_refused() -> None:
    with pytest.raises(ValueError, match="verifier"):
        db_setup.statements(db_setup.Missing(role=True), "r", "d", None)


# ── SCRAM-SHA-256 verifier ──────────────────────────────────────────────────


def test_the_verifier_matches_rfc_7677() -> None:
    # RFC 7677 §3: user "user", password "pencil".
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    verifier = db_setup.scram_verifier("pencil", salt=salt, iterations=4096)
    head, keys = verifier.split("$", 1)[1].split("$")
    stored_key, server_key = (base64.b64decode(k) for k in keys.split(":"))
    assert head == "4096:W22ZaJ0SNY7soEsUEjb6gQ=="
    nonce = "rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
    auth = (f"n=user,r=rOprNGfwEbeRWgbNEkqO,r={nonce},s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
            f"c=biws,r={nonce}").encode()
    assert base64.b64encode(hmac.digest(server_key, auth, "sha256")).decode() == (
        "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4=")
    proof = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    client_key = bytes(a ^ b for a, b in zip(
        proof, hmac.digest(stored_key, auth, "sha256"), strict=True))
    assert hashlib.sha256(client_key).digest() == stored_key


def test_each_verifier_has_its_own_salt() -> None:
    assert db_setup.scram_verifier("same") != db_setup.scram_verifier("same")


@pytest.mark.parametrize(
    ("password", "prepared"),
    [
        ("plain ascii 123", b"plain ascii 123"),
        ("a b", b"a b"),                     # a non-ASCII space maps to a space
        ("so­ft", b"soft"),                  # mapped to nothing
        ("Ⅸ", b"IX"),                        # NFKC
        ("a\u0007b", b"a\x07b"),                  # prohibited: used as given
        ("אa", "אa".encode()),          # RandALCat with LCat: as given
    ],
)
def test_saslprep_as_postgresql_applies_it(password: str, prepared: bytes) -> None:
    assert db_setup._saslprep_bytes(password) == prepared


# ── Settings ────────────────────────────────────────────────────────────────


def test_the_admin_dsn_quotes_and_uses_the_same_database(tmp_path: Path) -> None:
    (tmp_path / "pw").write_text("p@ss:w/rd\n", encoding="utf-8")
    settings = Settings(db_password="x", db_host="h", db_port=5433, db_name="g",
                        db_admin_user="pg admin", db_admin_password_file=tmp_path / "pw")
    assert settings.admin_dsn() == "postgresql://pg%20admin:p%40ss%3Aw%2Frd@h:5433/g"


def test_the_admin_dsn_needs_the_admin_user() -> None:
    with pytest.raises(RuntimeError, match="GRAPHOS_DB_ADMIN_USER"):
        Settings(db_password="x").admin_dsn()

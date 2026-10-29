"""The database set-up graphos-api can do at start: what ``deploy/db-init/`` does.

The PostgreSQL image runs ``/docker-entrypoint-initdb.d/`` once, when it
creates an empty data directory; graphos-api is another image and cannot put
files there, so a deployment had to mount the db-init folder into the database
container. With ``GRAPHOS_DB_ADMIN_USER`` set (the image's ``POSTGRES_USER``,
or any role that may create extensions and roles), graphos-api does the same
set-up itself, before it opens its pool (ADR-0012):

- the extensions ``age``, ``pg_trgm`` and ``pg_stat_statements``;
- its own role (``GRAPHOS_DB_USER``), unprivileged, with the password graphos
  connects with, when the role does not exist;
- that role's grants: ``CONNECT`` and ``CREATE`` on the database, ``USAGE`` on
  ``ag_catalog`` and DML on its tables and sequences, ``pg_read_all_stats``,
  and ``SET`` on ``temp_file_limit``.

``graphos_meta`` is not created here: graphos-api creates it as its own role
(:func:`graphos.db.ensure_internal_schema`), so it owns it, as the db-init
scripts arranged.

Conservative on a database that is already set up (one made with the db-init
folder, say), so that it starts with nothing changed:

- graphos first signs in as its own role and looks. When nothing is missing it
  never signs in as the admin, so an admin password file that is stale or gone
  does no harm.
- Only what is missing is done, each grant checked against the ACL on record
  for the role: a grant already given is not given again (a re-grant can add a
  second ACL entry, or a role membership with another grantor).
- An existing role is never altered: not its password, attributes or settings.
  Changing the password would lock out its other clients; when graphos cannot
  sign in with the password it has, it says so and leaves the fix to a person.
- The set-up never stops a start: when it fails it logs why and graphos goes on
  as without it (the pool then fails as it would have, or works).

Secrets: the admin password is read when needed and not kept. A new role's
password goes to the server as a SCRAM-SHA-256 verifier, never in clear, so a
server that logs the statement (``log_statement``, or the statement of an
error) logs no password. Nothing here logs a password or a DSN.

Replicas starting together are serialised by an advisory lock taken in the
set-up's transaction; the second finds the work done.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import stringprep
import unicodedata
from dataclasses import dataclass, field

import asyncpg

from graphos.config import Settings

log = logging.getLogger(__name__)

EXTENSIONS = ("age", "pg_trgm", "pg_stat_statements")
# Where an extension goes: AGE's own schema is fixed (ag_catalog); the others
# in public, as the db-init folder creates them. Not left to the admin's
# search_path: db-init gives the superuser one that starts with ag_catalog,
# which would put them there.
EXTENSION_SCHEMAS = {"pg_trgm": "public", "pg_stat_statements": "public"}
STATS_ROLE = "pg_read_all_stats"
# The Cypher console bounds each query's temporary files (graphos.console).
PARAMETERS = ("temp_file_limit",)
DATABASE_PRIVILEGES = ("CONNECT", "CREATE")
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
SEQUENCE_PRIVILEGES = ("USAGE", "SELECT")
# What GRANT ... ON ALL TABLES IN SCHEMA covers.
TABLE_KINDS = ("r", "p", "v", "m", "f")

# pg_advisory_xact_lock key serialising replicas' set-up ("graphos-db-setup").
LOCK_KEY = 0x67726170686F7301

CONNECT_TIMEOUT_S = 30.0

# An unprivileged role: graphos needs none of these.
ROLE_ATTRIBUTES = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
ROLE_SEARCH_PATH = 'ag_catalog, "$user", public'


@dataclass
class Missing:
    """What the database lacks for graphos's role."""

    extensions: list[str] = field(default_factory=list)
    role: bool = False
    database: list[str] = field(default_factory=list)
    catalog_schema: bool = False
    catalog_tables: list[str] = field(default_factory=list)
    catalog_sequences: list[str] = field(default_factory=list)
    stats: bool = False
    parameters: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(
            self.extensions or self.role or self.database or self.catalog_schema
            or self.catalog_tables or self.catalog_sequences or self.stats or self.parameters
        )


@dataclass
class Report:
    """What :func:`ensure_database_setup` found and did (for logs and tests)."""

    configured: bool = False
    admin_used: bool = False
    done: list[str] = field(default_factory=list)
    error: str | None = None


# ── Inspecting ──────────────────────────────────────────────────────────────

# Each check reads the ACL on record (aclexplode, with the owner's default when
# none is stored), not the privileges a role ends up with: CONNECT that PUBLIC
# has is not a grant to graphos's role, and REVOKE ... FROM PUBLIC would take it.

_ROLE_SQL = "SELECT oid, rolsuper FROM pg_catalog.pg_roles WHERE rolname = $1;"

_EXTENSIONS_SQL = "SELECT extname FROM pg_catalog.pg_extension WHERE extname = ANY($1::text[]);"

_DATABASE_SQL = """
SELECT p.priv
  FROM unnest($2::text[]) AS p(priv)
 WHERE NOT EXISTS (
   SELECT 1
     FROM pg_catalog.pg_database d,
          aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a
    WHERE d.datname = current_database() AND a.grantee = $1 AND a.privilege_type = p.priv)
 ORDER BY p.priv;
"""

_SCHEMA_SQL = """
SELECT EXISTS (
  SELECT 1
    FROM pg_catalog.pg_namespace n,
         aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a
   WHERE n.nspname = 'ag_catalog' AND a.grantee = $1 AND a.privilege_type = 'USAGE'),
  EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = 'ag_catalog');
"""

_RELATIONS_SQL = """
SELECT c.relname
  FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'ag_catalog' AND c.relkind::text = ANY($2::text[])
   AND (SELECT count(DISTINCT a.privilege_type)
          FROM aclexplode(coalesce(c.relacl, acldefault($3::text::"char", c.relowner))) a
         WHERE a.grantee = $1 AND a.privilege_type = ANY($4::text[])) < cardinality($4::text[])
 ORDER BY c.relname;
"""

_STATS_SQL = """
SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members
                WHERE member = $1 AND roleid = to_regrole($2));
"""

_PARAMETERS_SQL = """
SELECT p.name
  FROM unnest($2::text[]) AS p(name)
 WHERE NOT EXISTS (
   SELECT 1
     FROM pg_catalog.pg_parameter_acl pa, aclexplode(pa.paracl) a
    WHERE pa.parname = p.name AND a.grantee = $1 AND a.privilege_type = 'SET')
 ORDER BY p.name;
"""


async def inspect(conn: asyncpg.Connection, role: str) -> Missing:
    """What the database lacks for ``role``: run as that role or as the admin."""
    missing = Missing()
    found = {str(r["extname"]) for r in await conn.fetch(_EXTENSIONS_SQL, list(EXTENSIONS))}
    missing.extensions = [e for e in EXTENSIONS if e not in found]
    row = await conn.fetchrow(_ROLE_SQL, role)
    if row is None:
        missing.role = True
        return missing
    if row["rolsuper"]:
        return missing
    oid = row["oid"]
    missing.database = [
        str(r["priv"]) for r in await conn.fetch(_DATABASE_SQL, oid, list(DATABASE_PRIVILEGES))
    ]
    usage, catalog = await conn.fetchrow(_SCHEMA_SQL, oid)
    if catalog:
        missing.catalog_schema = not usage
        missing.catalog_tables = [
            str(r["relname"]) for r in await conn.fetch(
                _RELATIONS_SQL, oid, list(TABLE_KINDS), "r", list(TABLE_PRIVILEGES))
        ]
        missing.catalog_sequences = [
            str(r["relname"]) for r in await conn.fetch(
                _RELATIONS_SQL, oid, ["S"], "s", list(SEQUENCE_PRIVILEGES))
        ]
    missing.stats = not await conn.fetchval(_STATS_SQL, oid, STATS_ROLE)
    missing.parameters = [
        str(r["name"]) for r in await conn.fetch(_PARAMETERS_SQL, oid, list(PARAMETERS))
    ]
    return missing


# ── Doing ───────────────────────────────────────────────────────────────────


def quote_ident(name: str) -> str:
    """An SQL identifier, quoted."""
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """An SQL string literal (``standard_conforming_strings``, on since 9.1)."""
    return "'" + value.replace("'", "''") + "'"


def statements(missing: Missing, role: str, database: str, verifier: str | None) -> list[str]:
    """The SQL that gives ``role`` what is missing, extensions first.

    ``verifier`` is the role's SCRAM verifier, needed only when the role is
    created. Grants on ``ag_catalog`` assume its tables were inspected after
    the extensions were created.
    """
    r = quote_ident(role)
    sql = [
        f"CREATE EXTENSION IF NOT EXISTS {quote_ident(e)}"
        + (f" SCHEMA {quote_ident(EXTENSION_SCHEMAS[e])};" if e in EXTENSION_SCHEMAS else ";")
        for e in missing.extensions
    ]
    if missing.role:
        if verifier is None:
            raise ValueError("a new role needs its password verifier")
        sql += [
            f"CREATE ROLE {r} {ROLE_ATTRIBUTES} PASSWORD {quote_literal(verifier)};",
            f"ALTER ROLE {r} SET search_path = {ROLE_SEARCH_PATH};",
        ]
    if missing.database:
        sql.append(
            f"GRANT {', '.join(missing.database)} ON DATABASE {quote_ident(database)} TO {r};"
        )
    if missing.catalog_schema:
        sql.append(f"GRANT USAGE ON SCHEMA ag_catalog TO {r};")
    sql += [
        f"GRANT {', '.join(TABLE_PRIVILEGES)} ON ag_catalog.{quote_ident(t)} TO {r};"
        for t in missing.catalog_tables
    ]
    sql += [
        f"GRANT {', '.join(SEQUENCE_PRIVILEGES)} ON SEQUENCE ag_catalog.{quote_ident(s)} TO {r};"
        for s in missing.catalog_sequences
    ]
    if missing.stats:
        sql.append(f"GRANT {quote_ident(STATS_ROLE)} TO {r};")
    sql += [f"GRANT SET ON PARAMETER {p} TO {r};" for p in missing.parameters]
    return sql


def describe(missing: Missing) -> list[str]:
    """What :func:`statements` does, in words for the log (no secrets)."""
    done = [f"extension {e}" for e in missing.extensions]
    if missing.role:
        done.append("role")
    done += [f"{p} on the database" for p in missing.database]
    if missing.catalog_schema:
        done.append("USAGE on ag_catalog")
    done += [f"DML on ag_catalog.{t}" for t in missing.catalog_tables]
    done += [f"USAGE, SELECT on ag_catalog.{s}" for s in missing.catalog_sequences]
    if missing.stats:
        done.append(STATS_ROLE)
    done += [f"SET on {p}" for p in missing.parameters]
    return done


async def apply(conn: asyncpg.Connection, role: str, database: str, password: str) -> list[str]:
    """As the admin: give ``role`` what is missing, in one transaction; what was done."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1);", LOCK_KEY)
        missing = await inspect(conn, role)
        if not missing:
            return []
        # The extensions and the role first: ag_catalog's tables exist only once
        # AGE does, and a role's grants can be read only once it exists.
        first = Missing(extensions=missing.extensions, role=missing.role)
        verifier = scram_verifier(password) if missing.role else None
        for statement in statements(first, role, database, verifier):
            await conn.execute(statement)
        grants = await inspect(conn, role)
        for statement in statements(grants, role, database, None):
            await conn.execute(statement)
    return describe(first) + describe(grants)


# ── At start ────────────────────────────────────────────────────────────────


async def _connect(dsn: str) -> asyncpg.Connection:
    return await asyncpg.connect(dsn=dsn, timeout=CONNECT_TIMEOUT_S)


async def probe(settings: Settings) -> tuple[bool, Missing]:
    """As graphos's own role: whether it can sign in, and what is missing."""
    try:
        conn = await _connect(settings.dsn())
    except (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError):
        return False, Missing(role=True)
    try:
        return True, await inspect(conn, settings.db_user)
    finally:
        await conn.close()


async def ensure_database_setup(settings: Settings) -> Report:
    """Set the database up for graphos's role, when an admin role is configured.

    Without ``GRAPHOS_DB_ADMIN_USER`` it does nothing. Otherwise it looks as
    graphos's own role first and signs in as the admin only when something is
    missing. Never raises for a failed set-up: it logs and returns.
    """
    report = Report(configured=settings.db_admin_user is not None)
    if settings.db_admin_user is None:
        return report
    try:
        signed_in, missing = await probe(settings)
    except (OSError, asyncpg.PostgresError) as exc:
        # The pool will meet the same; it reports it as before.
        report.error = f"{type(exc).__name__}: {exc}"
        log.warning("database set-up: could not look as graphos's role",
                    extra={"error": report.error})
        return report
    if signed_in and not missing:
        log.info("database set-up: nothing to do", extra={"role": settings.db_user})
        return report

    report.admin_used = True
    try:
        password = settings.resolved_db_password()
        conn = await _connect(settings.admin_dsn())
    except (OSError, RuntimeError, asyncpg.PostgresError) as exc:
        report.error = f"{type(exc).__name__}: {exc}"
        log.error(
            "database set-up: could not sign in as the admin role; starting without the "
            "set-up (check GRAPHOS_DB_ADMIN_USER and GRAPHOS_DB_ADMIN_PASSWORD_FILE)",
            extra={"admin": settings.db_admin_user, "error": report.error},
        )
        return report
    try:
        report.done = await apply(conn, settings.db_user, settings.db_name, password)
    except asyncpg.PostgresError as exc:
        report.error = f"{type(exc).__name__}: {exc}"
        log.error(
            "database set-up failed and was rolled back; starting without it",
            extra={"admin": settings.db_admin_user, "role": settings.db_user,
                   "error": report.error},
        )
        return report
    finally:
        await conn.close()

    if report.done:
        log.warning("database set-up: done", extra={"role": settings.db_user,
                                                   "done": report.done})
    if not signed_in and "role" not in report.done:
        log.error(
            "graphos could not sign in as its database role, which exists: its password is "
            "not the one in GRAPHOS_DB_PASSWORD(_FILE). graphos never changes an existing "
            "role's password; as a superuser: ALTER ROLE <role> PASSWORD '...'",
            extra={"role": settings.db_user},
        )
    return report


# ── SCRAM-SHA-256 verifier (RFC 5802, RFC 7677; PostgreSQL's format) ────────

SCRAM_ITERATIONS = 4096


def scram_verifier(password: str, *, salt: bytes | None = None,
                   iterations: int = SCRAM_ITERATIONS) -> str:
    """The verifier PostgreSQL stores for ``password``, as ``psql``'s
    ``\\password`` and ``createuser -P`` send it: the server never sees the
    password itself."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", _saslprep_bytes(password), salt, iterations)
    client_key = hmac.digest(salted, b"Client Key", "sha256")
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.digest(salted, b"Server Key", "sha256")
    salt64, stored64, server64 = (
        base64.b64encode(b).decode("ascii") for b in (salt, stored_key, server_key)
    )
    return f"SCRAM-SHA-256${iterations}:{salt64}${stored64}:{server64}"


_PROHIBITED = (
    stringprep.in_table_c12, stringprep.in_table_c21_c22, stringprep.in_table_c3,
    stringprep.in_table_c4, stringprep.in_table_c5, stringprep.in_table_c6,
    stringprep.in_table_c7, stringprep.in_table_c8, stringprep.in_table_c9,
)


def _saslprep_bytes(password: str) -> bytes:
    """SASLprep (RFC 4013) of the password, as PostgreSQL applies it: when it
    is refused (a prohibited or ill-ordered character), the password as given."""
    raw = password.encode("utf-8")
    mapped = "".join(
        " " if stringprep.in_table_c12(c) else c
        for c in password if not stringprep.in_table_b1(c)
    )
    prepared = unicodedata.normalize("NFKC", mapped)
    if not prepared or any(check(c) for c in prepared for check in _PROHIBITED):
        return raw
    # RandALCat characters: then no LCat at all, and one at both ends.
    if any(stringprep.in_table_d1(c) for c in prepared) and (
        any(stringprep.in_table_d2(c) for c in prepared)
        or not stringprep.in_table_d1(prepared[0])
        or not stringprep.in_table_d1(prepared[-1])
    ):
        return raw
    return prepared.encode("utf-8")

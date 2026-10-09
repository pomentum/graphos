"""Identity and access: users, grants, the tokens graphos signs, and sessions.

**Users** are people and service accounts that sign in with a name and a
password (a service account's password is its secret).  Only a scrypt hash
is stored.  ``POST /v1/auth/login`` exchanges the password for a short-lived
access token (a JWT graphos signs, ES256); people also get a refresh token
for their browser session, kept in page memory by the UI and rotated on each
use.  Nothing graphos issues lasts: there are no static tokens.

**Grants** say what a subject (``user:<name>``) may do: a level on a tenant
pattern (``acme``, ``sol_*``, ``*``), or a global right (``create_tenants``
on a pattern, ``admin``).  Levels are ordered, each including the ones
before it::

    read < write < maintain (schema, recompute, metrics) < owner (grants, unregister)

**Changes apply at once.**  A token carries its user's ``token_version``;
changing a password, resetting it or disabling the user bumps the version,
and older tokens are refused at the next request.  Grants are read from
memory on every request.  Each replica keeps users and grants in memory and
reloads them on a ``NOTIFY`` on :data:`ACCESS_CHANNEL` (sent by the replica
that made the change), and every :data:`RELOAD_S` seconds in case a
notification was missed.

**Bootstrap.**  With no user, the first start creates ``graphos`` (the
``admin`` grant) with the password ``graphos``, or the one in
``GRAPHOS_ADMIN_PASSWORD_FILE``, as databases such as Neo4j do.  It must be
changed at the first sign-in: until then its tokens allow only changing it,
and graphos logs a warning at every start.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from pathlib import Path
from typing import Any

import asyncpg
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from graphos.oidc import Identity

log = logging.getLogger(__name__)

ACCESS_CHANNEL = "graphos_access"
# Users and grants are reloaded at least this often, notification or not.
RELOAD_S = 60.0
AUDIENCE = "graphos"
# The bootstrap admin and its well-known first password (changed at first sign-in).
ADMIN_USER = "graphos"
DEFAULT_ADMIN_PASSWORD = "graphos"


# ── Levels and principals ────────────────────────────────────────────────


class Level(IntEnum):
    """What a subject may do on a tenant; each level includes the lower ones."""

    READ = 1
    WRITE = 2
    MAINTAIN = 3
    OWNER = 4

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: str | Level) -> Level:
        if isinstance(value, Level):
            return value
        try:
            return cls[value.upper()]
        except KeyError:
            raise ValueError(f"not a level: {value!r}") from None


GLOBAL_RIGHTS = ("create_tenants", "admin")
GRANT_LEVELS = tuple(level.label for level in Level) + GLOBAL_RIGHTS

# Tenant names (a letter, then letters, digits, "_") plus "*" for patterns.
_PATTERN = re.compile(r"^[A-Za-z_*][A-Za-z0-9_*]{0,62}$")


def valid_pattern(pattern: str) -> bool:
    return bool(_PATTERN.fullmatch(pattern))


def matches(pattern: str, tenant: str) -> bool:
    """``*`` matches any run of characters; everything else is literal."""
    if pattern == "*":
        return True
    if "*" not in pattern:
        return pattern == tenant
    regex = "".join(".*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", pattern))
    return re.fullmatch(regex, tenant) is not None


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling and what they may do.

    ``subject`` is ``user:<name>`` (None for the anonymous admin of a server
    without auth).  ``must_change``: the password must be changed before
    anything else.
    """

    consumer: str
    subject: str | None = None
    admin: bool = False
    levels: tuple[tuple[str, Level], ...] = ()
    create_tenants: tuple[str, ...] = ()
    must_change: bool = False
    kind: str = "person"
    # How it signed in: "password" (a graphos user) or "oidc" (the provider).
    via: str = "password"

    def level(self, tenant: str) -> Level | None:
        """The highest level on ``tenant``; ``OWNER`` for an admin."""
        if self.admin:
            return Level.OWNER
        found = [level for pattern, level in self.levels if matches(pattern, tenant)]
        return max(found) if found else None

    def has(self, tenant: str, needed: Level) -> bool:
        level = self.level(tenant)
        return level is not None and level >= needed

    def can_create(self, tenant: str) -> bool:
        return self.admin or any(matches(p, tenant) for p in self.create_tenants)

    def visible(self, tenants: Iterable[str], at_least: Level = Level.READ) -> list[str]:
        """The tenants among ``tenants`` this principal holds at least ``at_least`` on."""
        return sorted(t for t in tenants if self.has(t, at_least))

    @classmethod
    def static(
        cls,
        consumer: str,
        *,
        tenants: Mapping[str, Level | str] | None = None,
        admin: bool = False,
        create_tenants: Sequence[str] = (),
    ) -> Principal:
        """A principal built in code (tests, the anonymous admin)."""
        return cls(
            consumer=consumer,
            subject=f"user:{consumer}",
            admin=admin,
            levels=tuple((t, Level.parse(v)) for t, v in (tenants or {}).items()),
            create_tenants=tuple(create_tenants),
        )


ANONYMOUS_ADMIN = Principal(consumer="anonymous", admin=True)


def subject_of(user: str) -> str:
    return f"user:{user}"


# ── Passwords ────────────────────────────────────────────────────────────

# scrypt cost: about 50 ms and 32 MiB per check (OWASP's minimum is N=2^17
# with r=8 for interactive logins on dedicated hardware; logins here are
# rare and rate-limited, and the API shares its host).
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM, dklen=32,
    )
    b64 = base64.urlsafe_b64encode
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.urlsafe_b64decode(digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.urlsafe_b64decode(salt),
            n=int(n), r=int(r), p=int(p), maxmem=_SCRYPT_MAXMEM, dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Checked against for unknown users, so a wrong name takes as long as a
# wrong password.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def new_secret() -> str:
    """A random password or secret, shown once."""
    return secrets.token_urlsafe(24)


class PasswordPolicyError(ValueError):
    pass


def check_policy(name: str, password: str, *, min_length: int, current: str | None = None) -> None:
    if len(password) < min_length:
        raise PasswordPolicyError(f"the password needs at least {min_length} characters")
    if password.lower() == name.lower():
        raise PasswordPolicyError("the password cannot be the user name")
    if current is not None and password == current:
        raise PasswordPolicyError("the new password must differ from the current one")


# ── Users and grants (stored, cached per replica) ────────────────────────


_USER_NAME = re.compile(r"^[a-z][a-z0-9_.\-]{0,62}$")


def valid_user_name(name: str) -> bool:
    return bool(_USER_NAME.fullmatch(name))


@dataclass(frozen=True, slots=True)
class User:
    name: str
    kind: str
    must_change: bool
    disabled: bool
    token_version: int
    created_at: datetime
    created_by: str | None
    last_login_at: datetime | None


@dataclass(frozen=True, slots=True)
class Grant:
    subject: str
    tenant_pattern: str
    level: str
    granted_by: str | None
    granted_at: datetime


class AuthError(Exception):
    """Sign-in refused.  The message is safe to show: it never says which
    part was wrong."""


REFUSED_MESSAGE = "wrong user name or password, or the account is locked or disabled"


@dataclass
class _Snapshot:
    users: dict[str, User] = field(default_factory=dict)
    grants: dict[str, list[Grant]] = field(default_factory=dict)


_USER_COLUMNS = (
    "name, kind, must_change, disabled, token_version, created_at, created_by, last_login_at"
)


class AccessStore:
    """Users and grants: stored in ``graphos_meta``, served from memory."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._snapshot = _Snapshot()
        self._lock = threading.Lock()
        self._reload_task: asyncio.Task[None] | None = None
        self._loop_task: asyncio.Task[None] | None = None

    # -- cache --

    async def load(self) -> None:
        async with self._pool.acquire() as conn:
            users = await conn.fetch(f"SELECT {_USER_COLUMNS} FROM graphos_meta.users;")
            grants = await conn.fetch(
                "SELECT subject, tenant_pattern, level, granted_by, granted_at "
                "FROM graphos_meta.grants;",
            )
        snapshot = _Snapshot(users={r["name"]: User(**dict(r)) for r in users})
        for r in grants:
            snapshot.grants.setdefault(r["subject"], []).append(Grant(**dict(r)))
        with self._lock:
            self._snapshot = snapshot

    def on_notify(self, _payload: str = "") -> None:
        """A change elsewhere: reload (coalesced)."""
        if self._reload_task is None or self._reload_task.done():
            self._reload_task = asyncio.get_running_loop().create_task(self._reload_logged())

    async def _reload_logged(self) -> None:
        try:
            await self.load()
        except Exception:  # noqa: BLE001 — the periodic reload retries
            log.warning("reloading users and grants failed", exc_info=True)

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop(), name="graphos-access-reload")

    async def stop(self) -> None:
        for task in (self._loop_task, self._reload_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(RELOAD_S)
            await self._reload_logged()

    async def _changed(self, conn: asyncpg.Connection) -> None:
        """After a write: tell the other replicas (on commit) and reload here."""
        await conn.execute("SELECT pg_notify($1, '');", ACCESS_CHANNEL)

    # -- reads (memory) --

    def user(self, name: str) -> User | None:
        with self._lock:
            return self._snapshot.users.get(name)

    def users(self) -> list[User]:
        with self._lock:
            return sorted(self._snapshot.users.values(), key=lambda u: u.name)

    def grants(self, subject: str | None = None, tenant: str | None = None) -> list[Grant]:
        with self._lock:
            pools = (
                [self._snapshot.grants.get(subject, [])] if subject is not None
                else list(self._snapshot.grants.values())
            )
        out = [g for gs in pools for g in gs]
        if tenant is not None:
            out = [
                g for g in out
                if g.level not in GLOBAL_RIGHTS and matches(g.tenant_pattern, tenant)
            ]
        return sorted(out, key=lambda g: (g.subject, g.tenant_pattern, g.level))

    def principal(self, name: str) -> Principal | None:
        """The principal of an enabled user, or None."""
        user = self.user(name)
        if user is None or user.disabled:
            return None
        return _principal(
            name, subject_of(name), self.grants(subject_of(name)),
            must_change=user.must_change, kind=user.kind, via="password",
        )

    def principal_for(self, identity: Identity, *, kind: str = "person") -> Principal:
        """The principal of someone signed in at the identity provider: the
        grants of its subject, groups, roles and client together.  Its
        ownership grants are made to ``oidc-sub:<sub>``."""
        subjects = identity.subjects()
        grants = [g for subject in subjects for g in self.grants(subject)]
        return _principal(identity.consumer, subjects[0], grants, kind=kind, via="oidc")

    # -- writes --

    async def create_user(
        self, name: str, kind: str, *, by: str | None, password: str | None = None,
        must_change: bool | None = None,
    ) -> tuple[User, str]:
        """Create a user; returns it and its password (generated unless given).
        A person must change a generated password at the first sign-in."""
        secret = password or new_secret()
        change = (kind == "person") if must_change is None else must_change
        hashed = await asyncio.to_thread(hash_password, secret)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"INSERT INTO graphos_meta.users "
                f"(name, kind, password_hash, must_change, created_by) "
                f"VALUES ($1, $2, $3, $4, $5) ON CONFLICT (name) DO NOTHING "
                f"RETURNING {_USER_COLUMNS};",
                name, kind, hashed, change, by,
            )
            if row is None:
                raise ValueError(f"user '{name}' already exists")
            await self._changed(conn)
        await self.load()
        return User(**dict(row)), secret

    async def set_password(
        self, name: str, password: str, *, must_change: bool,
    ) -> None:
        """Set a password, refuse every token issued before, end all sessions."""
        hashed = await asyncio.to_thread(hash_password, password)
        async with self._pool.acquire() as conn, conn.transaction():
            done = await conn.execute(
                "UPDATE graphos_meta.users SET password_hash = $2, must_change = $3, "
                "token_version = token_version + 1, failed_logins = 0, locked_until = NULL "
                "WHERE name = $1;",
                name, hashed, must_change,
            )
            if done == "UPDATE 0":
                raise KeyError(name)
            await revoke_sessions(conn, name)
            await self._changed(conn)
        await self.load()

    async def set_disabled(self, name: str, disabled: bool) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            done = await conn.execute(
                "UPDATE graphos_meta.users SET disabled = $2, "
                "token_version = token_version + 1 WHERE name = $1;",
                name, disabled,
            )
            if done == "UPDATE 0":
                raise KeyError(name)
            if disabled:
                await revoke_sessions(conn, name)
            await self._changed(conn)
        await self.load()

    async def delete_user(self, name: str) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            done = await conn.execute("DELETE FROM graphos_meta.users WHERE name = $1;", name)
            if done == "DELETE 0":
                raise KeyError(name)
            await conn.execute(
                "DELETE FROM graphos_meta.grants WHERE subject = $1;", subject_of(name),
            )
            await revoke_sessions(conn, name)
            await self._changed(conn)
        await self.load()

    async def grant(self, subject: str, pattern: str, level: str, *, by: str | None) -> bool:
        """Add a grant; False when it already existed."""
        async with self._pool.acquire() as conn, conn.transaction():
            done = await conn.execute(
                "INSERT INTO graphos_meta.grants (subject, tenant_pattern, level, granted_by) "
                "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING;",
                subject, pattern, level, by,
            )
            await self._changed(conn)
        await self.load()
        return bool(done != "INSERT 0 0")

    async def revoke(self, subject: str, pattern: str, level: str) -> bool:
        async with self._pool.acquire() as conn, conn.transaction():
            done = await conn.execute(
                "DELETE FROM graphos_meta.grants "
                "WHERE subject = $1 AND tenant_pattern = $2 AND level = $3;",
                subject, pattern, level,
            )
            await self._changed(conn)
        await self.load()
        return bool(done != "DELETE 0")

    async def drop_tenant(self, tenant: str) -> None:
        """A tenant was unregistered: its exact grants go (patterns stay)."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM graphos_meta.grants "
                "WHERE tenant_pattern = $1 AND level NOT IN ('create_tenants', 'admin');",
                tenant,
            )
            await self._changed(conn)
        await self.load()

    # -- sign-in --

    async def check_password(
        self, name: str, password: str, *, max_failures: int, lockout_s: int,
    ) -> User:
        """The user, when the password is right and the account usable.

        Raises:
            AuthError: anything else, with one message whatever the cause.
        """
        refused = AuthError(REFUSED_MESSAGE)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT password_hash, disabled, locked_until > now() AS locked "
                "FROM graphos_meta.users WHERE name = $1;",
                name,
            )
        stored = row["password_hash"] if row else _DUMMY_HASH
        ok = await asyncio.to_thread(verify_password, password, stored)
        if row is None:
            raise refused
        if row["disabled"] or row["locked"]:
            log.warning("sign-in refused: account disabled or locked", extra={"user": name})
            raise refused
        async with self._pool.acquire() as conn:
            if not ok:
                failures = await conn.fetchval(
                    "UPDATE graphos_meta.users SET "
                    "  locked_until = CASE WHEN failed_logins + 1 >= $2 "
                    "    THEN now() + make_interval(secs => $3) ELSE locked_until END, "
                    "  failed_logins = CASE WHEN failed_logins + 1 >= $2 "
                    "    THEN 0 ELSE failed_logins + 1 END "
                    "WHERE name = $1 RETURNING failed_logins;",
                    name, max_failures, float(lockout_s),
                )
                log.warning(
                    "sign-in refused: wrong password",
                    extra={"user": name, "locked": failures == 0},
                )
                raise refused
            await conn.execute(
                "UPDATE graphos_meta.users SET failed_logins = 0, locked_until = NULL, "
                "last_login_at = now() WHERE name = $1;",
                name,
            )
        user = self.user(name)
        if user is None:          # created on another replica a moment ago
            await self.load()
            user = self.user(name)
        if user is None:
            raise refused
        return user

    # -- bootstrap --

    async def bootstrap(self, password_file: Path | None) -> str | None:
        """Create the bootstrap admin when no user exists: ``"default"`` (the
        well-known password), ``"file"`` (``GRAPHOS_ADMIN_PASSWORD_FILE``), or
        None when users exist.  Safe when several replicas start at once."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('graphos.bootstrap_admin', 0));",
            )
            if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM graphos_meta.users);"):
                if password_file is not None:
                    log.info(
                        "GRAPHOS_ADMIN_PASSWORD_FILE is read only when no user exists; "
                        "it is ignored now (change the password in the UI or with "
                        "python -m graphos reset-admin-password)",
                    )
                return None
            if password_file is not None:
                password = password_file.read_text(encoding="utf-8").strip()
                if not password:
                    raise RuntimeError(f"GRAPHOS_ADMIN_PASSWORD_FILE {password_file} is empty")
                source = "file"
            else:
                password, source = DEFAULT_ADMIN_PASSWORD, "default"
            hashed = await asyncio.to_thread(hash_password, password)
            await conn.execute(
                "INSERT INTO graphos_meta.users "
                "(name, kind, password_hash, must_change, created_by) "
                "VALUES ($1, 'person', $2, true, 'bootstrap');",
                ADMIN_USER, hashed,
            )
            await conn.execute(
                "INSERT INTO graphos_meta.grants (subject, tenant_pattern, level, granted_by) "
                "VALUES ($1, '*', 'admin', 'bootstrap') ON CONFLICT DO NOTHING;",
                subject_of(ADMIN_USER),
            )
            await self._changed(conn)
        await self.load()
        return source

    def first_password_unchanged(self) -> bool:
        """The bootstrap admin still has its first password."""
        user = self.user(ADMIN_USER)
        return user is not None and user.must_change and not user.disabled


def _principal(
    consumer: str, subject: str, grants: list[Grant], *, kind: str, via: str,
    must_change: bool = False,
) -> Principal:
    return Principal(
        consumer=consumer,
        subject=subject,
        admin=any(g.level == "admin" for g in grants),
        levels=tuple(
            (g.tenant_pattern, Level.parse(g.level)) for g in grants
            if g.level not in GLOBAL_RIGHTS
        ),
        create_tenants=tuple(g.tenant_pattern for g in grants if g.level == "create_tenants"),
        must_change=must_change,
        kind=kind,
        via=via,
    )


# ── Signing keys and tokens ──────────────────────────────────────────────

# The ``sub`` prefix of graphos tokens issued to someone who signed in at
# the identity provider; their identity travels in the ``idn`` claim.
OIDC_SUB_PREFIX = "oidc:"


class TokenError(Exception):
    pass


class Signer:
    """Signs graphos's access tokens and verifies them (ES256)."""

    def __init__(self, *, issuer: str, ttl_s: int) -> None:
        self.issuer = issuer
        self.ttl_s = ttl_s
        self._signing: tuple[str, ec.EllipticCurvePrivateKey] | None = None
        self._public: dict[str, ec.EllipticCurvePublicKey] = {}

    async def load(self, pool: asyncpg.Pool, key_file: Path | None = None) -> None:
        """The newest key signs, every unretired key verifies.  Generates the
        first key (once, whichever replica gets there first)."""
        if key_file is not None:
            pem = key_file.read_bytes()
            key = serialization.load_pem_private_key(pem, password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey):
                raise RuntimeError("GRAPHOS_AUTH_SIGNING_KEY_FILE must hold an EC P-256 key")
            kid = "file-" + hashlib.sha256(pem).hexdigest()[:12]
            self._use([(kid, key)])
            return
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('graphos.signing_keys', 0));",
            )
            rows = await conn.fetch(
                "SELECT kid, private_pem FROM graphos_meta.signing_keys "
                "WHERE retired_at IS NULL ORDER BY created_at DESC;",
            )
            if not rows:
                key = ec.generate_private_key(ec.SECP256R1())
                new_pem = key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode()
                kid = uuid.uuid4().hex[:16]
                await conn.execute(
                    "INSERT INTO graphos_meta.signing_keys (kid, private_pem) VALUES ($1, $2);",
                    kid, new_pem,
                )
                rows = [{"kid": kid, "private_pem": new_pem}]
        keys = []
        for row in rows:
            key = serialization.load_pem_private_key(row["private_pem"].encode(), password=None)
            assert isinstance(key, ec.EllipticCurvePrivateKey)
            keys.append((row["kid"], key))
        self._use(keys)

    def use_key(self, kid: str, key: ec.EllipticCurvePrivateKey) -> None:
        """Sign and verify with ``key`` only (tests)."""
        self._use([(kid, key)])

    def _use(self, keys: list[tuple[str, ec.EllipticCurvePrivateKey]]) -> None:
        self._signing = keys[0]
        self._public = {kid: key.public_key() for kid, key in keys}

    def issue(self, user: User) -> tuple[str, int]:
        """An access token for ``user`` and its lifetime in seconds."""
        return self._sign({"sub": user.name, "ver": user.token_version})

    def issue_for(self, identity: Identity) -> tuple[str, int]:
        """An access token for someone who signed in at the identity provider."""
        return self._sign({"sub": OIDC_SUB_PREFIX + identity.subject, "idn": identity.claims()})

    def _sign(self, extra: dict[str, Any]) -> tuple[str, int]:
        if self._signing is None:
            raise RuntimeError("no signing key loaded")
        kid, key = self._signing
        now = int(time.time())
        claims = {
            "iss": self.issuer, "aud": AUDIENCE,
            "iat": now, "nbf": now, "exp": now + self.ttl_s, "jti": uuid.uuid4().hex,
            **extra,
        }
        return jwt.encode(claims, key, algorithm="ES256", headers={"kid": kid}), self.ttl_s

    def verify(self, token: str) -> dict[str, Any]:
        """The claims of a valid token this graphos signed.

        Raises:
            TokenError: not one (bad signature, expired, wrong issuer or audience).
        """
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            key = self._public.get(kid) if isinstance(kid, str) else None
            if key is None:
                raise TokenError("unknown signing key")
            return jwt.decode(
                token, key, algorithms=["ES256"], audience=AUDIENCE, issuer=self.issuer,
                leeway=30, options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise TokenError(str(exc)) from None


# ── Browser sessions (refresh tokens, people only) ───────────────────────


class SessionError(Exception):
    pass


def _hash_refresh(token: str) -> str:
    # A refresh token is 256 random bits: a plain hash is enough to keep the
    # stored value useless to whoever reads the table.
    return hashlib.sha256(token.encode()).hexdigest()


async def open_session(  # noqa: PLR0913 — a session's fields
    pool: asyncpg.Pool, user: str, *, idle_s: int, max_s: int,
    identity: Identity | None = None, id_token: str | None = None,
) -> str:
    """A new session; ``identity`` for one signed in at the identity provider
    (its tokens are issued again from it at each renewal)."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    await pool.execute(
        "INSERT INTO graphos_meta.sessions "
        "(family, user_name, refresh_hash, idle_expires_at, absolute_expires_at, kind, claims, "
        " id_token) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8);",
        uuid.uuid4(), user, _hash_refresh(token),
        now + timedelta(seconds=idle_s), now + timedelta(seconds=max_s),
        "oidc" if identity else "native",
        json.dumps(identity.claims()) if identity else None,
        id_token,
    )
    return token


@dataclass(frozen=True)
class Renewed:
    user: str
    # None when a token rotated a moment ago was presented again (see
    # REUSE_GRACE_S): a new access token, no new refresh token.
    refresh_token: str | None
    identity: Identity | None          # set for a session signed in at the provider


# Seconds during which a just-rotated refresh token still renews the access
# token (without rotating again) instead of revoking the session: two browser
# tabs sharing the session cookie may renew at the same moment.
REUSE_GRACE_S = 10


async def rotate_session(pool: asyncpg.Pool, token: str, *, idle_s: int) -> Renewed:
    """The session's user and a new refresh token, for a valid refresh token.

    A token already rotated (used twice: someone else has it) revokes its
    whole family.

    Raises:
        SessionError: unknown, expired, revoked or reused.
    """
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT id, family, user_name, rotated_at, revoked_at, kind, claims::text AS claims, "
            "  id_token, "
            "  idle_expires_at < now() OR absolute_expires_at < now() AS expired, "
            "  absolute_expires_at, "
            "  rotated_at > now() - make_interval(secs => $2) AS just_rotated "
            "FROM graphos_meta.sessions WHERE refresh_hash = $1 FOR UPDATE;",
            _hash_refresh(token), float(REUSE_GRACE_S),
        )
        if row is None:
            raise SessionError("unknown session")
        family_live = row["just_rotated"] and await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM graphos_meta.sessions WHERE family = $1 "
            "  AND rotated_at IS NULL AND revoked_at IS NULL "
            "  AND idle_expires_at >= now() AND absolute_expires_at >= now());",
            row["family"],
        )
        if family_live and row["revoked_at"] is None:
            # Another tab renewed a moment ago: an access token, no rotation.
            identity = (
                Identity.from_claims(json.loads(row["claims"])) if row["kind"] == "oidc" else None
            )
            return Renewed(user=row["user_name"], refresh_token=None, identity=identity)
        if row["rotated_at"] is not None:
            # Committed before refusing: the revocation must outlive this call.
            await conn.execute(
                "UPDATE graphos_meta.sessions SET revoked_at = now() "
                "WHERE family = $1 AND revoked_at IS NULL;",
                row["family"],
            )
            log.warning(
                "a rotated refresh token was used again: session revoked",
                extra={"user": row["user_name"]},
            )
            refused: SessionError | None = SessionError("session revoked")
        elif row["revoked_at"] is not None or row["expired"]:
            refused = SessionError("session ended")
        else:
            refused = None
        new = ""
        if refused is None:
            new = secrets.token_urlsafe(32)
            await conn.execute(
                "UPDATE graphos_meta.sessions SET rotated_at = now() WHERE id = $1;", row["id"],
            )
            await conn.execute(
                "INSERT INTO graphos_meta.sessions "
                "(family, user_name, refresh_hash, idle_expires_at, absolute_expires_at, "
                " kind, claims, id_token) "
                "VALUES ($1, $2, $3, LEAST(now() + make_interval(secs => $4), $5), $5, "
                " $6, $7::jsonb, $8);",
                row["family"], row["user_name"], _hash_refresh(new), float(idle_s),
                row["absolute_expires_at"], row["kind"], row["claims"], row["id_token"],
            )
        # Rotated rows are only kept to detect reuse while the family lives.
        await conn.execute(
            "DELETE FROM graphos_meta.sessions "
            "WHERE absolute_expires_at < now() - interval '1 day';",
        )
    if refused is not None:
        raise refused
    identity = Identity.from_claims(json.loads(row["claims"])) if row["kind"] == "oidc" else None
    return Renewed(user=row["user_name"], refresh_token=new, identity=identity)


@dataclass(frozen=True)
class Ended:
    kind: str                   # native | oidc
    id_token: str | None        # the provider's ID token, for signing out there


async def close_session(pool: asyncpg.Pool, token: str) -> Ended | None:
    """Ends the session; None when there was none to end."""
    row = await pool.fetchrow(
        "WITH ended AS (UPDATE graphos_meta.sessions s SET revoked_at = now() "
        "FROM graphos_meta.sessions t WHERE t.refresh_hash = $1 AND s.family = t.family "
        "AND s.revoked_at IS NULL RETURNING s.kind, s.id_token) "
        "SELECT kind, id_token FROM ended LIMIT 1;",
        _hash_refresh(token),
    )
    return Ended(kind=row["kind"], id_token=row["id_token"]) if row else None


async def revoke_sessions(conn: asyncpg.Connection, user: str) -> None:
    await conn.execute(
        "UPDATE graphos_meta.sessions SET revoked_at = now() "
        "WHERE user_name = $1 AND revoked_at IS NULL;",
        user,
    )


# ── Sign-in throttling (per client address, per replica) ────────────────


class Throttle:
    """At most ``per_min`` attempts a minute from one address."""

    def __init__(self, per_min: int, *, max_keys: int = 10_000) -> None:
        self.per_min = per_min
        self._max_keys = max_keys
        self._seen: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def admit(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._seen) >= self._max_keys and key not in self._seen:
                self._seen = {k: v for k, v in self._seen.items() if v and v[-1] > now - 60}
            times = self._seen.setdefault(key, deque())
            while times and times[0] <= now - 60:
                times.popleft()
            if len(times) >= self.per_min:
                return False
            times.append(now)
            return True

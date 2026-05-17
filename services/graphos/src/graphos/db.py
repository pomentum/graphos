"""asyncpg connection-pool factory.

Holds the single application-wide pool. Loaded into ``app.state.pool`` by the
lifespan hook (see app.py).
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import asyncpg

from graphos.config import Settings

log = logging.getLogger(__name__)


async def _on_connect(conn: asyncpg.Connection) -> None:
    """Per-physical-connection initialisation — runs ONCE when asyncpg
    opens a new backend connection.

    ``LOAD 'age'`` belongs here: a loaded shared library persists for the
    whole session and is *not* undone by the ``RESET ALL`` asyncpg issues
    on pool release, so reloading it on every acquire would be wasted work.

    The search_path does NOT belong here — see ``_on_acquire``.

    Best-effort: only a superuser may ``LOAD`` an arbitrary library.  When
    graphos runs as an unprivileged role, AGE must be in the server's
    ``shared_preload_libraries`` (the bundled compose file does this), which
    makes the explicit load unnecessary.
    """
    with contextlib.suppress(asyncpg.exceptions.InsufficientPrivilegeError):
        await conn.execute("LOAD 'age';")


async def _on_acquire(conn: asyncpg.Connection) -> None:
    """Runs on EVERY ``pool.acquire()``.

    asyncpg resets a connection with ``RESET ALL`` when it is released back
    to the pool, which clears the ``search_path`` set at connect time. Any
    handler that uses bare ``agtype`` / ``->`` (e.g. ``get_node_by_external_id``)
    then fails with ``type "agtype" does not exist`` /
    ``operator does not exist: ag_catalog.agtype -> unknown`` on every reused
    connection. Re-applying the search_path here makes ``ag_catalog`` resolve
    on every acquired connection, not just freshly-opened ones.
    """
    await conn.execute('SET search_path = ag_catalog, "$user", public;')


# Server-side limit for schema work: tenant bootstrap (index builds on large
# labels can take minutes), the internal schema, and the startup reload.
DDL_TIMEOUT_S = 1800.0


async def allow_long_statements(conn: asyncpg.Connection, seconds: float) -> None:
    """Raise this connection's ``statement_timeout`` for a deliberately long call.

    Session-scoped: the pool's ``RESET ALL`` on release puts the default back,
    so the raise never leaks to the next request using the connection.
    """
    await conn.execute(f"SET statement_timeout = '{int(seconds * 1000)}ms'")


async def create_pool(settings: Settings) -> asyncpg.Pool:
    """Create the connection pool. Caller owns the pool lifecycle."""
    pool = await asyncpg.create_pool(
        dsn=settings.dsn(),
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        init=_on_connect,
        setup=_on_acquire,
        # Postgres enforces the timeout: ``statement_timeout`` is a startup
        # parameter, so the pool's ``RESET ALL`` on release restores it after
        # ``allow_long_statements`` raised it for a long operation.  No
        # client-side ``command_timeout``: it cannot be raised per connection
        # and would cut long operations (index builds, Louvain) short.
        server_settings={"statement_timeout": str(int(settings.statement_timeout_s * 1000))},
        command_timeout=None,
    )
    if pool is None:
        raise RuntimeError("asyncpg.create_pool returned None")
    log.info(
        "graphos-db pool ready",
        extra={"min": settings.db_pool_min_size, "max": settings.db_pool_max_size},
    )
    return pool


async def fetch_db_version(pool: asyncpg.Pool) -> dict[str, Any]:
    """Return postgres + AGE version metadata for /v1/info."""
    async with pool.acquire() as conn:
        pg_version: str = await conn.fetchval("SHOW server_version;")
        age_version: str | None = await conn.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'age';"
        )
    return {"postgres_version": pg_version, "age_version": age_version}


# ── graphos-internal state ──────────────────────────────────────────────────


_INTERNAL_SCHEMA_DDL = """
CREATE SCHEMA IF NOT EXISTS graphos_meta;

CREATE TABLE IF NOT EXISTS graphos_meta.tenants (
    name        text PRIMARY KEY,
    spec        jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Schema-agnostic K/V cache.  Per-tenant key namespace; value is
-- arbitrary JSON.  ``expires_at`` is the absolute deadline (TTL is
-- applied by the writer); lazy eviction on read (a stale row is
-- treated as absent and replaced on the next PUT).  No autovacuum
-- pressure to worry about — cached snapshots are kilobytes, hundreds
-- of keys per tenant at the top end.
--
-- We deliberately don't use postgres's ``pg_cron`` for eviction — the
-- in-process LRU on the read side already filters stale rows; the
-- only growth is unused keys that ``expires_at`` will never hide.
-- A periodic ``DELETE FROM graphos_meta.cache WHERE expires_at <
-- now() - interval '1 day'`` housekeeping can be added later if the
-- table grows unboundedly.
CREATE TABLE IF NOT EXISTS graphos_meta.cache (
    tenant      text NOT NULL,
    key         text NOT NULL,
    value       jsonb NOT NULL,
    expires_at  timestamptz,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, key)
);

-- Partial index for the staleness sweep ('expires_at' often NULL for
-- never-expire entries; the partial keeps the index narrow).
CREATE INDEX IF NOT EXISTS cache_expires_at
    ON graphos_meta.cache (expires_at)
    WHERE expires_at IS NOT NULL;
"""


async def ensure_internal_schema(pool: asyncpg.Pool) -> None:
    """Idempotent setup of the `graphos_meta` schema for graphos's own state.

    This schema isn't tenant-specific — it's the substrate graphos-api
    uses to remember tenant registrations across container restarts.
    Layer-1 init SQL also creates it on fresh volumes; this routine
    makes upgrade-on-existing-volume work too.
    """
    async with pool.acquire() as conn:
        await allow_long_statements(conn, DDL_TIMEOUT_S)
        await conn.execute(_INTERNAL_SCHEMA_DDL)
    log.info("graphos_meta schema ensured")

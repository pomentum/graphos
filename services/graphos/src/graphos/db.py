"""asyncpg connection-pool factory.

Holds the single application-wide pool. Loaded into ``app.state.pool`` by the
lifespan hook (see app.py).
"""

from __future__ import annotations

import contextlib
import logging
import re
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
        # Keep asyncpg's default reset on release (pg_advisory_unlock_all and
        # RESET ALL): it lets go of a schema-write lock whose unlock was cut
        # short (graphos.schema_version).
        # Postgres enforces the timeout: ``statement_timeout`` is a startup
        # parameter, so the pool's ``RESET ALL`` on release restores it after
        # ``allow_long_statements`` raised it for a long operation.  No
        # client-side ``command_timeout``: it cannot be raised per connection
        # and would cut long operations (index builds, Louvain) short.
        # JIT off unless configured: on graphos's heaviest queries it gained
        # nothing measurable, and on tenants without statistics it spent
        # ~1.8 s compiling queries that run in milliseconds.
        # Custom plans always: graphos filters on property expressions whose
        # selectivity the planner can judge only from the values. After five
        # runs of a prepared statement PostgreSQL 18 kept a generic plan for a
        # property scan that read 34,702 rows for 100 (50 ms -> 1 s).
        server_settings={
            "statement_timeout": str(int(settings.statement_timeout_s * 1000)),
            "jit": "on" if settings.db_jit else "off",
            "plan_cache_mode": "force_custom_plan",
        },
        command_timeout=None,
    )
    if pool is None:
        raise RuntimeError("asyncpg.create_pool returned None")
    log.info(
        "graphos-db pool ready",
        extra={"min": settings.db_pool_min_size, "max": settings.db_pool_max_size},
    )
    return pool


# The oldest database graphos starts on (ADR-0007), the one it ships with
# (docker-compose.graphos.yml). PostgreSQL 16 has no AGE after 1.6, which carries
# out some Cypher writes in a read-only transaction and can wedge a database
# process on a variable-length pattern (docs/guide/apache-age.md).
MIN_POSTGRES_MAJOR = 18
MIN_AGE = (1, 8)


def _leading(version: str | None, parts: int) -> tuple[int, ...]:
    """The first ``parts`` numbers of a version (``18beta1`` → ``(18,)``,
    ``1.8.0-rc1`` → ``(1, 8)``); ``()`` when it starts with none."""
    found = re.match(r"\s*(\d+)(?:\.(\d+))?", version or "")
    if not found:
        return ()
    return tuple(int(n) for n in found.groups()[:parts] if n is not None)


def unsupported_database(postgres_version: str, age_version: str | None) -> str | None:
    """Why graphos does not start on this PostgreSQL / AGE pair, or ``None``."""
    major = (_leading(postgres_version, 1) or (0,))[0]
    age = _leading(age_version, 2)
    if major < MIN_POSTGRES_MAJOR or age < MIN_AGE:
        return (
            f"PostgreSQL {major} with AGE {age_version or '(none)'} is not supported: graphos "
            f"needs PostgreSQL {MIN_POSTGRES_MAJOR} with AGE {'.'.join(map(str, MIN_AGE))} or "
            "later. To move, see docs/guide/upgrade-postgresql-18.md"
        )
    return None


# AGE identifies a graph by the OID of its schema: create_graph sets
# ag_graph.graphid to namespace::oid, and ag_label.graph points at it. A
# pg_dump restored into another database gives every schema a new OID but
# restores the catalogue rows as they were, so graphid keeps the old one and
# Cypher fails ("graph with oid N does not exist") while plain SQL works.
# Moving both tables in one statement keeps fk_graph_oid satisfied (a NO
# ACTION key is checked at the end of the statement), so the role graphos-api
# runs as, which may update AGE's catalogue but does not own it, can do it.
_AGE_CATALOGUE_REPAIR_SQL = """
WITH moved AS (
  SELECT graphid AS old_id, namespace::oid AS new_id
    FROM ag_catalog.ag_graph WHERE graphid <> namespace::oid
), graphs AS (
  UPDATE ag_catalog.ag_graph g SET graphid = m.new_id
    FROM moved m WHERE g.graphid = m.old_id
  RETURNING g.name
), labels AS (
  UPDATE ag_catalog.ag_label l SET graph = m.new_id
    FROM moved m WHERE l.graph = m.old_id
)
SELECT name FROM graphs ORDER BY name;
"""


async def repair_age_catalogue(pool: asyncpg.Pool) -> list[str]:
    """Point AGE's catalogue at each graph's schema where a restore left it
    on the old OID; the graphs repaired.

    Runs at start-up, before any tenant is loaded. A role that may not update
    the catalogue gets a logged error naming the manual fix
    (docs/guide/upgrade-postgresql-18.md) and graphos starts anyway: its SQL
    reads and writes do not need the catalogue's ids, only the Cypher console
    does.
    """
    async with pool.acquire() as conn:
        try:
            names = [str(r["name"]) for r in await conn.fetch(_AGE_CATALOGUE_REPAIR_SQL)]
        except asyncpg.PostgresError as exc:
            stale = await conn.fetchval(
                "SELECT count(*) FROM ag_catalog.ag_graph WHERE graphid <> namespace::oid;"
            )
            log.error(
                "AGE catalogue points at old schema OIDs (a restored database) and could "
                "not be repaired; Cypher on these graphs fails until the catalogue fix in "
                "docs/guide/upgrade-postgresql-18.md is run",
                extra={"graphs": stale, "error": f"{type(exc).__name__}: {exc}"},
            )
            return []
    if names:
        log.warning(
            "AGE catalogue repaired: graphs pointed at their schemas' new OIDs (a restored "
            "database)",
            extra={"graphs": names},
        )
    return names


async def fetch_db_version(pool: asyncpg.Pool) -> dict[str, Any]:
    """The PostgreSQL and AGE versions, for /v1/info and the start-up check."""
    async with pool.acquire() as conn:
        pg_version: str = await conn.fetchval("SHOW server_version;")
        age_version: str | None = await conn.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'age';"
        )
    return {
        "postgres_version": pg_version,
        "age_version": age_version,
    }


# ── graphos-internal state ──────────────────────────────────────────────────


_INTERNAL_SCHEMA_DDL = """
CREATE SCHEMA IF NOT EXISTS graphos_meta;

CREATE TABLE IF NOT EXISTS graphos_meta.tenants (
    name        text PRIMARY KEY,
    spec        jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Relationship endpoints a tenant declares (``graphos.endpoints``): the label
-- pairs each edge type may join.  Kept apart from ``tenants.spec``: a server
-- that predates them rewrites the whole spec on re-registration and would
-- drop them (a rolling upgrade, a rollback); it never touches this table.
CREATE TABLE IF NOT EXISTS graphos_meta.edge_endpoints (
    tenant      text NOT NULL,
    edge_type   text NOT NULL,
    src_label   text NOT NULL,
    dst_label   text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, edge_type, src_label, dst_label)
);

-- Per-tenant schema settings, kept apart from the spec for the same reason.
CREATE TABLE IF NOT EXISTS graphos_meta.tenant_settings (
    tenant            text PRIMARY KEY,
    strict_endpoints  boolean NOT NULL DEFAULT false,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
-- The tenant's schema manager (ADR-0013, ``graphos.schema_drift``): the one
-- subject whose registrations may change its structure; NULL: none.
ALTER TABLE graphos_meta.tenant_settings ADD COLUMN IF NOT EXISTS schema_manager text;
ALTER TABLE graphos_meta.tenant_settings ADD COLUMN IF NOT EXISTS schema_manager_since timestamptz;

-- Labels, edge types and declared pairs dropped or removed (ADR-0013): a
-- registration that declares one again creates it again, and its dry run's
-- drift report says so.  A row goes when the item is registered again.
CREATE TABLE IF NOT EXISTS graphos_meta.schema_removals (
    tenant      text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('vertex_label', 'edge_type', 'edge_endpoint')),
    name        text NOT NULL,
    src_label   text NOT NULL DEFAULT '',
    dst_label   text NOT NULL DEFAULT '',
    removed_at  timestamptz NOT NULL DEFAULT now(),
    removed_by  text,
    PRIMARY KEY (tenant, kind, name, src_label, dst_label)
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

-- Property names each vertex label's nodes carry (``graphos.node_properties``),
-- added in the same transaction as the nodes; read by the resolver's catalogue.
-- Only grows: a name stays after its last carrier is gone.
CREATE TABLE IF NOT EXISTS graphos_meta.node_properties (
    tenant    text NOT NULL,
    label     text NOT NULL,
    property  text NOT NULL,
    PRIMARY KEY (tenant, label, property)
);

-- Tenants whose node_properties rows cover every node: graphs created after
-- the table existed at once, older ones after one backfill.
CREATE TABLE IF NOT EXISTS graphos_meta.node_properties_complete (
    tenant  text PRIMARY KEY,
    since   timestamptz NOT NULL DEFAULT now()
);

-- The community map (``graphos.community_map``): written with each Louvain
-- recompute from the edges it clustered, read by GET .../communities/map.
CREATE TABLE IF NOT EXISTS graphos_meta.community_map (
    tenant          text NOT NULL,
    community_id    integer NOT NULL,
    size            integer NOT NULL,
    labels          jsonb NOT NULL,
    hub_gid         bigint,
    hub_degree      integer NOT NULL DEFAULT 0,
    internal_edges  bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant, community_id)
);

CREATE TABLE IF NOT EXISTS graphos_meta.community_links (
    tenant  text NOT NULL,
    src     integer NOT NULL,
    dst     integer NOT NULL,
    type    text NOT NULL,
    count   bigint NOT NULL,
    PRIMARY KEY (tenant, src, dst, type)
);

CREATE TABLE IF NOT EXISTS graphos_meta.community_map_runs (
    tenant       text PRIMARY KEY,
    computed_at  timestamptz NOT NULL,
    communities  integer NOT NULL,
    modularity   double precision
);

-- The last written run of each analysis (``metrics``, ``communities``) and
-- the change-feed position it started from: the writes after that position
-- are what its results do not know about (``graphos.changes.written_since``).
CREATE TABLE IF NOT EXISTS graphos_meta.analysis_runs (
    tenant       text NOT NULL,
    kind         text NOT NULL,
    computed_at  timestamptz NOT NULL,
    txid         xid8 NOT NULL,
    seq          bigint NOT NULL,
    scope        jsonb,
    PRIMARY KEY (tenant, kind)
);
-- How the run computed (communities: algorithm, resolution, weights), so a
-- Leiden run knows whether it may start from the stored partition (ADR-0011).
ALTER TABLE graphos_meta.analysis_runs ADD COLUMN IF NOT EXISTS params jsonb;

-- Background jobs (``graphos.jobs``): long operations a caller asked to
-- run asynchronously.  ``tenant`` is NULL for instance-wide jobs (vacuum).
-- ``heartbeat_at`` is refreshed by the process running the job; a running
-- job whose heartbeat stops is marked failed by any replica.
CREATE TABLE IF NOT EXISTS graphos_meta.jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             text NOT NULL,
    tenant           text,
    status           text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    params           jsonb NOT NULL DEFAULT '{}'::jsonb,
    result           jsonb,
    error            jsonb,
    consumer         text,
    cancel_requested boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    heartbeat_at     timestamptz,
    finished_at      timestamptz
);
-- A synchronous call of a job kind, listed while it runs (graphos.jobs.run_tracked).
ALTER TABLE graphos_meta.jobs ADD COLUMN IF NOT EXISTS sync boolean NOT NULL DEFAULT false;

-- Claiming the oldest queued job, and the stale-heartbeat sweep.
CREATE INDEX IF NOT EXISTS jobs_active
    ON graphos_meta.jobs (status, created_at)
    WHERE status IN ('queued', 'running');

-- Retention sweep and per-tenant listing.
CREATE INDEX IF NOT EXISTS jobs_finished_at
    ON graphos_meta.jobs (finished_at)
    WHERE finished_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_tenant_created
    ON graphos_meta.jobs (tenant, created_at DESC);

-- At most one whole-graph job (community recompute or graph metrics) queued
-- or running per tenant: both rewrite every node (graphos.analysis).
DROP INDEX IF EXISTS graphos_meta.jobs_one_active_recompute;
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_graph_job
    ON graphos_meta.jobs (tenant)
    WHERE kind IN ('recompute_communities', 'compute_metrics')
      AND status IN ('queued', 'running');

-- How much of a long job is done (graphos.jobs.report_progress): {done, total}.
ALTER TABLE graphos_meta.jobs ADD COLUMN IF NOT EXISTS progress jsonb;

-- At most one property rename queued or running per tenant (graphos.schema_rename).
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_rename
    ON graphos_meta.jobs (tenant)
    WHERE kind = 'rename_property' AND status IN ('queued', 'running');

-- The change feed (``graphos.changes``): one row per write, inserted in the
-- write's own transaction.  A record's position is (txid, seq); readers see
-- only rows of transactions older than every one still running, so no
-- position is ever skipped.  The primary key serves the per-tenant reads.
CREATE TABLE IF NOT EXISTS graphos_meta.changes (
    tenant     text NOT NULL,
    txid       xid8 NOT NULL DEFAULT pg_current_xact_id(),
    seq        bigint GENERATED ALWAYS AS IDENTITY,
    at         timestamptz NOT NULL DEFAULT now(),
    op         text NOT NULL,
    kind       text NOT NULL,
    label      text,
    src_label  text,
    dst_label  text,
    ids        jsonb,
    rev        bigint,
    count      integer NOT NULL,
    detail     jsonb,
    consumer   text,
    trace_id   text,
    PRIMARY KEY (tenant, txid, seq)
);
CREATE INDEX IF NOT EXISTS changes_at ON graphos_meta.changes (at);

-- The highest pruned position per tenant: a cursor behind it gets 410.
CREATE TABLE IF NOT EXISTS graphos_meta.change_horizon (
    tenant  text PRIMARY KEY,
    txid    xid8 NOT NULL,
    seq     bigint NOT NULL
);

-- Identity and access (graphos.identity).  People and service accounts
-- sign in with a password or secret, stored only as a scrypt hash.
-- ``token_version`` is carried in every token graphos signs; bumping it
-- (password change, reset, disable) refuses the tokens issued before, so a
-- change applies at once, not at token expiry.
CREATE TABLE IF NOT EXISTS graphos_meta.users (
    name           text PRIMARY KEY,
    kind           text NOT NULL CHECK (kind IN ('person', 'service')),
    password_hash  text NOT NULL,
    must_change    boolean NOT NULL DEFAULT false,
    disabled       boolean NOT NULL DEFAULT false,
    failed_logins  integer NOT NULL DEFAULT 0,
    locked_until   timestamptz,
    token_version  integer NOT NULL DEFAULT 0,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text,
    last_login_at  timestamptz
);

-- What a subject may do: a level on a tenant pattern ("acme", "sol_*",
-- "*"), or a global right (create_tenants on a pattern, admin on "*").
CREATE TABLE IF NOT EXISTS graphos_meta.grants (
    subject         text NOT NULL,
    tenant_pattern  text NOT NULL,
    level           text NOT NULL CHECK (level IN
                        ('read', 'write', 'maintain', 'owner', 'create_tenants', 'admin')),
    granted_by      text,
    granted_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (subject, tenant_pattern, level)
);

-- Browser sessions of people: one rotating refresh token each (only its
-- hash is kept).  A rotated token used again revokes its whole family.
CREATE TABLE IF NOT EXISTS graphos_meta.sessions (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    family               uuid NOT NULL,
    user_name            text NOT NULL,
    refresh_hash         text NOT NULL UNIQUE,
    created_at           timestamptz NOT NULL DEFAULT now(),
    idle_expires_at      timestamptz NOT NULL,
    absolute_expires_at  timestamptz NOT NULL,
    rotated_at           timestamptz,
    revoked_at           timestamptz
);
-- A session signed in through the identity provider keeps the claims its
-- tokens are issued from (subject, name, groups, roles).
ALTER TABLE graphos_meta.sessions ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'native';
ALTER TABLE graphos_meta.sessions ADD COLUMN IF NOT EXISTS claims jsonb;
-- The provider's ID token, kept server-side only, sent back as id_token_hint
-- when signing out there (providers otherwise ask the person to confirm).
ALTER TABLE graphos_meta.sessions ADD COLUMN IF NOT EXISTS id_token text;
-- The MCP client a session belongs to (graphos.oauth, ADR-0008): renewed only by
-- that client, at /oauth/token, and its tokens only read.
ALTER TABLE graphos_meta.sessions ADD COLUMN IF NOT EXISTS client text;
CREATE INDEX IF NOT EXISTS sessions_family ON graphos_meta.sessions (family);

-- A sign-in at the identity provider in progress: graphos keeps the PKCE
-- verifier and the nonce, so the browser stores nothing.  Single use, 5 min.
CREATE TABLE IF NOT EXISTS graphos_meta.oidc_pending (
    state          text PRIMARY KEY,
    nonce          text NOT NULL,
    code_verifier  text NOT NULL,
    redirect_uri   text NOT NULL,
    return_to      text NOT NULL,
    expires_at     timestamptz NOT NULL
);

-- MCP clients that registered themselves (RFC 7591, ADR-0008, graphos.oauth).
-- All public clients, bound by PKCE, so no client secret is kept.
CREATE TABLE IF NOT EXISTS graphos_meta.oauth_clients (
    client_id   text PRIMARY KEY,
    info        jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
-- The last sign-in or renewal: unused clients are forgotten (graphos.oauth).
ALTER TABLE graphos_meta.oauth_clients ADD COLUMN IF NOT EXISTS last_used_at timestamptz;
-- Their authorization codes: only the hash is kept, single use, 5 minutes.
CREATE TABLE IF NOT EXISTS graphos_meta.oauth_codes (
    code_hash   text PRIMARY KEY,
    client_id   text NOT NULL,
    user_name   text NOT NULL,
    data        jsonb NOT NULL,
    expires_at  timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON graphos_meta.sessions (user_name);

-- Keys graphos signs its tokens with (ES256).  The newest unretired key
-- signs; every unretired key verifies.
CREATE TABLE IF NOT EXISTS graphos_meta.signing_keys (
    kid          text PRIMARY KEY,
    private_pem  text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    retired_at   timestamptz
);

-- Tenants unregistered with their data kept (``graphos.bootstrap``): the
-- registration (spec, declared endpoints, settings) waits here to be
-- restored.  A table of its own, not a flag on ``tenants``: a server that
-- predates it (a rollback, a rolling upgrade) never reads it, so it cannot
-- bring an unregistered tenant back.
CREATE TABLE IF NOT EXISTS graphos_meta.unregistered_tenants (
    name              text PRIMARY KEY,
    spec              jsonb NOT NULL,
    endpoints         jsonb NOT NULL DEFAULT '[]'::jsonb,
    strict_endpoints  boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL,
    unregistered_at   timestamptz NOT NULL DEFAULT now(),
    unregistered_by   text
);
-- Its schema manager, restored with it (ADR-0014): unregistering is not a way round it.
ALTER TABLE graphos_meta.unregistered_tenants ADD COLUMN IF NOT EXISTS schema_manager text;
ALTER TABLE graphos_meta.unregistered_tenants
    ADD COLUMN IF NOT EXISTS schema_manager_since timestamptz;

-- Saved Cypher queries of the console (graphos.api.cypher), per tenant:
-- the query, its parameters and how to show it, as one JSON document.
CREATE TABLE IF NOT EXISTS graphos_meta.saved_queries (
    tenant      text NOT NULL,
    name        text NOT NULL,
    definition  jsonb NOT NULL,
    created_by  text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, name)
);

-- What a signed-in person chose in the web UI (theme, ...): one JSON
-- document per subject, only names graphos knows (``graphos.preferences``).
CREATE TABLE IF NOT EXISTS graphos_meta.user_preferences (
    subject     text PRIMARY KEY,
    prefs       jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
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

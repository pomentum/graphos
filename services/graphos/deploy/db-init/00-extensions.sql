-- graphos-db init script — Layer 1 (tenant-agnostic).
-- Runs once on first volume creation (postgres convention: scripts in
-- /docker-entrypoint-initdb.d execute only when the data dir is empty).
--
-- This file MUST NOT contain tenant-specific content (create_graph,
-- create_vlabel, create_elabel, indexes). Tenant schema is the
-- responsibility of graphos-api's Layer-2 bootstrap (see src/graphos/bootstrap.py).

CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Ensure AGE is loaded on every new session for the graphos role.
ALTER ROLE graphos SET search_path = ag_catalog, "$user", public;

-- Grant base permissions on ag_catalog (AGE places its system tables there).
GRANT USAGE ON SCHEMA ag_catalog TO graphos;

-- graphos-side persistent state lives in its own schema, separate from
-- AGE's per-tenant graphs. Created tenant-agnostic; tenant specs are
-- written by graphos-api's bootstrap path.
CREATE SCHEMA IF NOT EXISTS graphos_meta;

CREATE TABLE IF NOT EXISTS graphos_meta.tenants (
    name        text PRIMARY KEY,
    spec        jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);


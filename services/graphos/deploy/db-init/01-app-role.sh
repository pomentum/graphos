#!/usr/bin/env bash
# graphos-db init script — create the unprivileged role graphos-api uses.
#
# Runs once, on first volume creation, as the superuser created by the
# postgres image (POSTGRES_USER). graphos-api connects as `graphos_app`:
# not a superuser, cannot create roles or databases. It can create schemas
# in this database (AGE puts each tenant graph in its own schema), use AGE's
# catalog, own graphos_meta, and read query statistics for /v1/db/*.
#
# graphos-api does the same at start when GRAPHOS_DB_ADMIN_USER is set
# (src/graphos/db_setup.py, ADR-0012); keep the two in step
# (tests/test_db_setup_db.py compares the roles they make).
set -euo pipefail

pw_file=/run/graphos/secrets/graphos_app_pw.txt
if [[ ! -s "${pw_file}" ]]; then
  echo "01-app-role.sh: ${pw_file} missing; run scripts/init-secrets.sh" >&2
  exit 1
fi

psql -v ON_ERROR_STOP=1 -v app_pw="$(cat "${pw_file}")" \
     --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<'SQL'
CREATE ROLE graphos_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  PASSWORD :'app_pw';
ALTER ROLE graphos_app SET search_path = ag_catalog, "$user", public;

GRANT CONNECT, CREATE ON DATABASE :"DBNAME" TO graphos_app;

-- AGE keeps its graph and label registry in ag_catalog.
GRANT USAGE ON SCHEMA ag_catalog TO graphos_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ag_catalog TO graphos_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ag_catalog TO graphos_app;

-- graphos's own state.
ALTER SCHEMA graphos_meta OWNER TO graphos_app;
ALTER TABLE graphos_meta.tenants OWNER TO graphos_app;

-- /v1/db/slow-queries and /v1/db/stats.
GRANT pg_read_all_stats TO graphos_app;

-- The Cypher console bounds each query's temporary files (a superuser-level
-- setting; GRAPHOS_CYPHER_TEMP_FILE_LIMIT).
GRANT SET ON PARAMETER temp_file_limit TO graphos_app;
SQL

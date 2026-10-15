#!/usr/bin/env bash
# graphos-db-import: one-shot, on the PostgreSQL 18 / AGE 1.8 image.
#
# Loads the dump graphos-db-export made of the PostgreSQL 16 database into
# graphos-db (PostgreSQL 18), once: the graphs, graphos's own state (tenants,
# users, grants, saved queries, the change feed, revisions) and all.
# graphos-api starts only after it succeeds.
#
# The dump is restored into a database of its own and put in place of the
# new, empty `graphos` only once complete, so a failed import leaves nothing
# behind and the next `docker compose up` runs it again from the start. It
# never loads over a `graphos` database graphos has already used: data
# written since the move is never replaced by the older dump.
set -euo pipefail

IN=/upgrade                        # graphos_upgrade
DB=graphos
STAGE=graphos_import

say() { echo "graphos-db-import: $*"; }
done_() { date -u +%Y-%m-%dT%H:%M:%SZ > "${IN}/imported"; }

if [[ -e "${IN}/imported" ]]; then
  say "already imported; nothing to do"
  exit 0
fi
if [[ ! -e "${IN}/exported" ]]; then
  say "nothing exported to import; nothing to do"
  exit 0
fi

export PGHOST=graphos-db PGUSER=graphos PGPASSWORD PGOPTIONS="-c client_min_messages=warning"
PGPASSWORD="$(tr -d '[:space:]' < /run/graphos/secrets/graphos_pw.txt)"

# A new database holds graphos_meta.tenants, empty, from the init scripts and
# nothing else of graphos's (and there is none when an import stopped while
# putting its database in place).
in_use=0
if [[ -n "$(psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '${DB}';")" ]]; then
  in_use="$(psql -d "${DB}" -tAc "
    SELECT (SELECT count(*) FROM pg_tables WHERE schemaname = 'graphos_meta' AND tablename <> 'tenants')
         + (SELECT count(*) FROM graphos_meta.tenants);")"
fi
if [[ "${in_use}" != "0" ]]; then
  say "graphos-db already holds graphos's data; not loading the PostgreSQL 16 dump over it"
  done_
  exit 0
fi

say "loading the PostgreSQL 16 dump into PostgreSQL 18"
psql -d postgres -v ON_ERROR_STOP=1 -q \
  -c "DROP DATABASE IF EXISTS ${STAGE} WITH (FORCE);" \
  -c "CREATE DATABASE ${STAGE};"
pg_restore -d "${STAGE}" --exit-on-error "${IN}/graphos.dump"

# AGE records each graph by its schema's OID (ag_graph.graphid, ag_label.graph),
# and a restore gives every schema a new one; the schema itself is restored by
# name (ag_graph.namespace). Point both at the new OID, as AGE's own
# age_finish_pg_upgrade() does after a pg_upgrade.
psql -d "${STAGE}" -v ON_ERROR_STOP=1 -q <<'SQL'
BEGIN;
ALTER TABLE ag_catalog.ag_label DROP CONSTRAINT IF EXISTS fk_graph_oid;
UPDATE ag_catalog.ag_label l SET graph = g.namespace::oid
  FROM ag_catalog.ag_graph g
 WHERE l.graph = g.graphid AND g.graphid <> g.namespace::oid;
UPDATE ag_catalog.ag_graph SET graphid = namespace::oid WHERE graphid <> namespace::oid;
ALTER TABLE ag_catalog.ag_label
  ADD CONSTRAINT fk_graph_oid FOREIGN KEY (graph) REFERENCES ag_catalog.ag_graph (graphid);
COMMIT;
SQL

# What a dump does not carry: grants on the database itself (the roles and
# their settings live outside it and were made by graphos-db's init).
psql -d postgres -v ON_ERROR_STOP=1 -q \
  -c "DROP DATABASE IF EXISTS ${DB} WITH (FORCE);" \
  -c "ALTER DATABASE ${STAGE} RENAME TO ${DB};"
psql -d "${DB}" -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'graphos_app') THEN
    EXECUTE 'GRANT CONNECT, CREATE ON DATABASE ${DB} TO graphos_app';
  END IF;
END \$\$;
SQL

graphs="$(psql -d "${DB}" -tAc 'SELECT count(*) FROM ag_catalog.ag_graph;')"
done_
say "done: ${graphs} graph(s) on PostgreSQL 18; the PostgreSQL 16 volume is kept as it was"

#!/usr/bin/env bash
# graphos-db-export: one-shot, on the PostgreSQL 16 / AGE 1.6 image.
#
# graphos-db moved to PostgreSQL 18, on a volume of its own. On the first
# start after that, this dumps the PostgreSQL 16 database the old volume
# (graphos_data) holds, for graphos-db-import to load into the new one. It
# starts PostgreSQL 16 on that volume, with no network, only to dump it, and
# stops it cleanly: the old volume keeps its data, as a way back.
#
# Every other start it has nothing to do and exits at once: a new install
# (no data), a volume that is not PostgreSQL 16, or an export already made.
set -euo pipefail

OLD=/var/lib/postgresql/data      # graphos_data
OUT=/upgrade                      # graphos_upgrade, shared with graphos-db-import

say() { echo "graphos-db-export: $*"; }

if [[ -e "${OUT}/exported" ]]; then
  say "already exported; nothing to do"
  exit 0
fi
if [[ ! -s "${OLD}/PG_VERSION" ]]; then
  say "no PostgreSQL data on the old volume (a new install); nothing to do"
  exit 0
fi
version="$(cat "${OLD}/PG_VERSION")"
if [[ "${version}" != "16" ]]; then
  say "the old volume holds PostgreSQL ${version}, not 16; nothing to do"
  exit 0
fi

export PGPASSWORD
PGPASSWORD="$(tr -d '[:space:]' < /run/graphos/secrets/graphos_pw.txt)"

say "dumping the PostgreSQL 16 database for the move to PostgreSQL 18"
gosu postgres pg_ctl -D "${OLD}" -w -t 600 \
  -o "-c listen_addresses='' -c shared_preload_libraries=age,pg_stat_statements" start
trap 'gosu postgres pg_ctl -D "${OLD}" -m fast -w stop' EXIT

pg_dump -h /var/run/postgresql -U graphos -d graphos -Fc -f "${OUT}/graphos.dump.part"
mv "${OUT}/graphos.dump.part" "${OUT}/graphos.dump"
date -u +%Y-%m-%dT%H:%M:%SZ > "${OUT}/exported"
say "done: $(du -h "${OUT}/graphos.dump" | cut -f1) dumped; graphos-db-import loads it"

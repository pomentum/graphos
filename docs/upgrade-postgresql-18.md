# Upgrading the database: PostgreSQL 16 / AGE 1.6 to PostgreSQL 18 / AGE 1.8

graphos's bundled database is now **PostgreSQL 18 with Apache AGE 1.8**. PostgreSQL 16 with AGE
1.6, which graphos 0.9 and earlier shipped, is **deprecated**: graphos still runs on it, logs
`database deprecated` at start and reports it in `GET /v1/info` (`database_deprecated`). Support
for it will be removed in a later release.

With Docker Compose the move is one command: update the files and run `docker compose up -d`.
The compose file moves the data by itself on the first start. This page says what that does, how
long it takes, how to check it and how to go back.

- [Why move](#why-move)
- [Before you start](#before-you-start)
- [Upgrade with Docker Compose](#upgrade-with-docker-compose)
- [What happens during the first start](#what-happens-during-the-first-start)
- [Check the result](#check-the-result)
- [Going back](#going-back)
- [Clean up](#clean-up)
- [Troubleshooting](#troubleshooting)
- [A database you run yourself](#a-database-you-run-yourself)
- [Why a dump and a restore, not pg_upgrade](#why-a-dump-and-a-restore-not-pg_upgrade)

## Why move

- **AGE 1.6 is the last AGE release for PostgreSQL 16.** Its fixes land only in AGE 1.8, released
  for PostgreSQL 15 and 18 (PostgreSQL 17's latest is AGE 1.7; checked 2026-10-15).
- **AGE 1.6 carries out Cypher `SET`, `REMOVE` and `DELETE` inside a read-only transaction.**
  graphos now refuses those clauses in its console before they reach the database, but anything
  else that runs Cypher against the database stays exposed on 1.6.
- **A variable-length pattern run directly against AGE 1.6 can wedge a database process** until
  it is killed (only `SIGKILL` stops it). On AGE 1.8 the same query fails at once with an error
  and the session stays usable. See [apache-age.md](apache-age.md).
- **It is faster.** On the same graph (251,000 nodes, 940,000 edges) and the same graphos, a mix
  of 12 reads ran in 67 s instead of 85 s with one user and in 35 s instead of 57 s with five.
  Paths and aggregate queries ran 1.7–2.3× faster.

graphos's API and data do not change: the same tenants, users, grants, saved queries, change
feed, revisions and sessions, the same requests and answers. (What else the release that brings
the move changes is in the [changelog](../CHANGELOG.md).)

## Before you start

- **Back up.** The move leaves the PostgreSQL 16 volume `graphos_data` as it was, but take your
  usual backup anyway (a snapshot of the volume, or `pg_dump`).
- **Disk.** The new volume needs about the size of the restored database, which is usually
  *smaller* than today's (a restore does not carry the old database's dead rows: a 9.5 GB
  database came back as 4.3 GB). The dump in between is compressed; plan for the size of the
  database again, free, to be safe.
- **Downtime.** graphos-api is down from the start of the move until it is ready again. The move
  takes about as long as a dump and a restore of the database, plus the statistics: measured on
  a laptop, a 9.5 GB database with five tenants in about 12 minutes; a 2 GB tenant restore in
  72 s plus 36 s of statistics.
- **Secrets.** Keep `services/graphos/deploy/secrets/` as it is: the new database is created
  with the same passwords.
- **Your own compose overrides.** If you override `graphos-db` (image, volume, command), check
  them against the new file: the database runs on a new volume, `graphos_data_pg18`, and sets
  `PGDATA`. The Louvain overlay (`docker-compose.louvain.yml`) needs no change.

## Upgrade with Docker Compose

```bash
# 1. The new release (or your own copy of its compose file and services/graphos/deploy/)
git pull

# 2. Start it: the first start moves the data
docker compose -f docker-compose.graphos.yml up -d

# 3. Follow the move
docker compose -f docker-compose.graphos.yml logs -f graphos-db-export graphos-db-import
```

That is all. Do not remove any volume before the move has finished and you have checked it.

## What happens during the first start

`docker compose up` stops the old PostgreSQL 16 `graphos-db` and then:

1. **`graphos-db`** starts PostgreSQL 18 / AGE 1.8 on the new, empty volume `graphos_data_pg18`;
   its init scripts create the extensions and graphos's roles, as for a new install.
2. **`graphos-db-export`** (one-shot, PostgreSQL 16 image, no network) starts PostgreSQL 16 on
   `graphos_data`, dumps the database to the volume `graphos_upgrade` (`pg_dump -Fc`) and stops
   it cleanly.
3. **`graphos-db-import`** (one-shot, PostgreSQL 18 image):
   - restores the dump into a database of its own;
   - points AGE's catalogue at the restored schemas (AGE records each graph by its schema's OID,
     which a restore changes);
   - only then puts it in place of the new, empty `graphos` database;
   - gathers planner statistics in parallel.
4. **`graphos-api`** waits for the import to succeed, then starts as usual.

Rules that keep the move safe:

- **Every later start has nothing to do.** Both one-shots find the move made and exit at once;
  so do they on a new install.
- **A failed import changes nothing graphos uses** (its half-restored copy is dropped by the next
  try), keeps `graphos-api` from starting, and runs again from the start on the next `up`.
- **The dump is never loaded over a database graphos has used**, so data written after the move
  is never replaced by older data, even if `graphos_upgrade` is removed while `graphos_data` is
  kept.
- **`graphos_data` is only read.** It stays as it was, as the way back.

The log lines to expect:

```text
graphos-db-export: dumping the PostgreSQL 16 database for the move to PostgreSQL 18
graphos-db-export: done: 287M dumped; graphos-db-import loads it
graphos-db-import: loading the PostgreSQL 16 dump into PostgreSQL 18
graphos-db-import: gathering planner statistics
graphos-db-import: done: 5 graph(s) on PostgreSQL 18; the PostgreSQL 16 volume is kept as it was
```

## Check the result

```bash
curl -s http://127.0.0.1:8080/readyz
curl -s http://127.0.0.1:8080/v1/info
```

- `/readyz` answers `{"status": "ready", ...}` with your tenant count.
- `/v1/info` shows `postgres_version` 18.x, `age_version` 1.8.x and `database_deprecated: null`.
- Sign in as usual: users, grants and sessions came across, and tokens issued before the move
  still verify (the signing key is in the database).
- Optionally compare a few counts with your own reads, or a dataset export of a tenant before and
  after.

## Going back

The previous release's compose file runs on `graphos_data`, untouched by the move:

```bash
git checkout <the previous release>          # its docker-compose.graphos.yml
docker compose -f docker-compose.graphos.yml up -d
```

It comes back as it was before the move: **what was written on PostgreSQL 18 since is not in
it.** To keep that, export the tenants first (`GET /v1/{tenant}/graph/snapshot`) and import them
(`POST /v1/{tenant}/graph/snapshot-import`) after going back.

## Clean up

Once you are content with the move:

```bash
docker volume rm graphos_data graphos_upgrade
```

`graphos_data` is the PostgreSQL 16 data (the way back); `graphos_upgrade` holds the dump and the
markers that say the move is done.

## Troubleshooting

| What you see | What it means | What to do |
|---|---|---|
| `graphos-api` does not start; `graphos-db-import` exited with an error | The restore failed (its log says why: often disk space) | Fix the cause and run `docker compose ... up -d` again: the import starts from the beginning |
| `graphos-db-import: graphos-db already holds graphos's data; not loading the PostgreSQL 16 dump over it` | The new database was already in use (the move was made before, or graphos ran on it) | Nothing: by design, the dump never replaces newer data |
| `graphos-db-export: the old volume holds PostgreSQL N, not 16` | `graphos_data` is not a PostgreSQL 16 volume | Nothing to move from it; check your volume names |
| `graphos-db-export: no PostgreSQL data on the old volume` | A new install, or `graphos_data` is empty | Nothing to move |
| `graphos-db` restarts with `database files are incompatible with server` | A PostgreSQL 16 volume was mounted on PostgreSQL 18 (an override that kept `graphos_data` on `graphos-db`) | Use the compose file's volumes: `graphos_data_pg18` for `graphos-db` |
| The first start is slow after the move | Planner statistics are gathered by the import; on very large databases it takes a while | Wait; later starts are quick |

## A database you run yourself

If graphos runs against a PostgreSQL you manage (not the bundled `graphos-db`), move it the same
way by hand:

```bash
# 1. Stop graphos-api. Dump the PostgreSQL 16 database (custom format).
pg_dump -h OLD -U graphos -d graphos -Fc -f graphos.dump

# 2. On PostgreSQL 18 with AGE 1.8 (shared_preload_libraries=age,pg_stat_statements),
#    with graphos's roles created as in services/graphos/deploy/db-init/01-app-role.sh:
createdb -h NEW -U graphos graphos
pg_restore -h NEW -U graphos -d graphos --exit-on-error graphos.dump   # serial: no -j / --jobs

# 3. AGE's catalogue, then planner statistics
psql -h NEW -U graphos -d graphos -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
ALTER TABLE ag_catalog.ag_label DROP CONSTRAINT IF EXISTS fk_graph_oid;
UPDATE ag_catalog.ag_label l SET graph = g.namespace::oid
  FROM ag_catalog.ag_graph g WHERE l.graph = g.graphid AND g.graphid <> g.namespace::oid;
UPDATE ag_catalog.ag_graph SET graphid = namespace::oid WHERE graphid <> namespace::oid;
ALTER TABLE ag_catalog.ag_label
  ADD CONSTRAINT fk_graph_oid FOREIGN KEY (graph) REFERENCES ag_catalog.ag_graph (graphid);
COMMIT;
GRANT CONNECT, CREATE ON DATABASE graphos TO graphos_app;
SQL
vacuumdb -h NEW -U graphos -d graphos --analyze-only --jobs 4

# 4. Point graphos-api at NEW (GRAPHOS_DB_HOST ...) and start it.
```

**Restore serially.** Do not pass `-j` / `--jobs` to `pg_restore`: AGE's catalogue tables
`ag_graph` and `ag_label` are then restored in parallel, `ag_label` can load first, and the restore
fails (`COPY failed for table "ag_label"` … `violates foreign key constraint "fk_graph_oid"`, and
a worker dies). The same dump restores correctly without `-j` (checked on PostgreSQL 18 / AGE 1.8:
the contents matched by checksum). Only the restore must be serial: a parallel dump
(`pg_dump -Fd -j N`) is fine.

[`deploy/db-upgrade/import-pg18.sh`](../services/graphos/deploy/db-upgrade/import-pg18.sh) is the
same procedure as the compose runs it. On a managed PostgreSQL that offers AGE 1.8 on PostgreSQL 18,
the same steps apply where its tools allow them (a serial `pg_restore`, then the catalogue fix);
check what it permits before moving.

## Why a dump and a restore, not pg_upgrade

A PostgreSQL 16 data directory cannot be opened by PostgreSQL 18: each major version has its own
on-disk format. PostgreSQL's in-place tool, `pg_upgrade`, does not fit here:

- **It needs both versions' binaries and both AGE builds in one image.** The official images
  carry one each.
- **It refuses AGE's catalogue.** `ag_graph.namespace` has the type `regnamespace`, which
  `pg_upgrade` rejects. AGE's helper for that, `age_prepare_pg_upgrade()`, exists from AGE 1.8
  only, so a 1.6 database cannot be prepared.
- **Upstream has not tested it across AGE versions.**

A dump and a restore work with the official images as they are, leave the old volume untouched as
the way back, and give a compact database. Once on AGE 1.8, a later major upgrade can use
`pg_upgrade` with AGE's prepare and finish functions.

# ADR-0016: The release compose file no longer moves PostgreSQL 16

- **Status:** Accepted (2026-11-22)
- **Scope:** deployment: `docker-compose.graphos.yml`, `scripts/run-db-tests.sh`, the upgrade and
  installation guides; not graphos-api, and not the scripts in `services/graphos/deploy/db-upgrade/`
- **Related:** [ADR-0007](ADR-0007-drop-postgresql-16.md) (whose "the move stays" this replaces for
  the compose file), [ADR-0012](ADR-0012-database-setup-at-start.md),
  [upgrade-postgresql-18.md](../upgrade-postgresql-18.md)

## Context

graphos 0.10 moved its database from PostgreSQL 16 with AGE 1.6 to PostgreSQL 18 with AGE 1.8.
To get existing installs across, the release compose file gained two one-shot services:
`graphos-db-export` (the PostgreSQL 16 image, no network) dumps the old volume `graphos_data` into
`graphos_upgrade`, and `graphos-db-import` loads the dump into PostgreSQL 18, before graphos-api
starts. ADR-0007 (0.11) stopped graphos-api from starting on PostgreSQL 16 and kept the move in the
compose file, so an install still on 16 would get to 18 with `docker compose up -d`.

Since then:

- every start of every install runs both one-shot containers, which have nothing to do on any
  install made since 0.10 or already moved, and pulls the PostgreSQL 16 image for them;
- the compose file declares two volumes (`graphos_data`, `graphos_upgrade`) that a new install
  creates for nothing, and graphos-api waits for the move to complete before it starts;
- the test harness has to rename and stub both services so a test stack cannot touch the old
  volume (`scripts/run-db-tests.sh`).

Installs still on PostgreSQL 16 are the ones that skipped every release from 0.10 to 0.16.0, each
of which moves them.

## Decision

- `docker-compose.graphos.yml` no longer has `graphos-db-export`, `graphos-db-import`, the volumes
  `graphos_data` and `graphos_upgrade`, nor graphos-api's wait for the move. Its database is
  `graphos_data_pg18`, as before.
- **An install still on PostgreSQL 16 upgrades in two steps:** first to 0.16.0, whose compose file
  moves the data (the upgrade guide, unchanged in substance), then to this release or later.
  Nothing else changes for it: the move itself is the same, and graphos_data stays as it was, as
  the way back.
- The scripts in `services/graphos/deploy/db-upgrade/` stay in the repository: the 0.16.0 compose
  file runs them, a database run by hand is moved with them (the upgrade guide's "a database you
  run yourself"), and `tests/test_age_cache_trigger_db.py` checks the import script's SQL.
- The test harness no longer renames or stubs the move's services and volumes.
- The db-init mount stays, as ADR-0012 decided; its open question (whether the release compose
  drops it once the move is retired) stays open and is not answered here.

## Decision options

| Option | Taken? | Why |
|---|---|---|
| **Drop the move from the release compose; upgrade through 0.16.0** | Yes | The common case (every install made or moved since 0.10) loses two containers, two volumes and a wait at every start; the rare one keeps a tested path, one release away. |
| Keep the move in the release compose (ADR-0007) | No | Every install pays at every start for a step almost none of them needs. |
| Move it into an optional overlay (`docker-compose.pg16-move.yml`), added with `-f` once | No | Another file to keep in step with the release compose for a path the published 0.16.0 already gives, tested as released. |
| Drop the scripts too | No | A database run by hand still needs them, and a test checks the import script. |

## Evidence

- The services and volumes removed, and graphos-api's `depends_on` on `graphos-db-import`, are the
  owner's change to `docker-compose.graphos.yml`; `docker compose config` accepts the result.
- `scripts/run-db-tests.sh` brings the test stack up from the new compose file and runs the
  database tests (the acceptance below).
- The 0.16.0 release (tag `0.16.0`) holds the compose file with the move, and the images it
  names are published.

## Consequences

- A new install creates one volume, starts two containers fewer, and graphos-api no longer waits
  for a move.
- An install still on PostgreSQL 16 that pulls this release's compose file directly gets a new,
  empty PostgreSQL 18 database; its data stays on `graphos_data`, untouched, and graphos-api
  starts on the empty one. The upgrade guide and the changelog say to go through 0.16.0 first;
  doing so afterwards still moves the data (the move refuses only a database graphos already
  holds data in, so a database used meanwhile must be emptied first, as the guide says).
- Docs: the upgrade guide (two steps), installation §1, §2 and §11, concept.md §2.4 and §8,
  features.md, usage.md, development.md's layout table, the service guide, the notices (the
  PostgreSQL 16 image is no longer used by the release compose), the CHANGELOG. ADR-0007's status
  notes the part this replaces.

## Open questions

None.

## Acceptance

- `docker compose -f docker-compose.graphos.yml config` is valid and names neither service nor
  volume of the move.
- The database tests pass through `scripts/run-db-tests.sh` on the new compose file.
- No guide tells a reader that this release's compose file moves PostgreSQL 16.

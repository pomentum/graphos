# ADR-0007: PostgreSQL 16 / AGE 1.6 is no longer supported

- **Status:** Accepted (2026-10-19)
- **Scope:** the databases graphos-api starts on, CI's integration job, `GET /v1/info`, the docs;
  not the move from PostgreSQL 16 (`docker-compose.graphos.yml`, `deploy/db-upgrade/`)
- **Related:** [upgrade-postgresql-18.md](../upgrade-postgresql-18.md); apache-age.md; concept.md
  §8; ADR-0003 (variable-length patterns)

## Context

graphos 0.10 moved its bundled database to PostgreSQL 18 with Apache AGE 1.8 and kept PostgreSQL
16 with AGE 1.6 as deprecated: graphos still started on it, logged `database deprecated` and
reported the reason in `GET /v1/info` (`database_deprecated`). CI ran the whole integration suite
twice, once per database.

AGE 1.6 is the last AGE for PostgreSQL 16; its fixes land only in 1.8. On 1.6:

- Cypher `SET`, `REMOVE` and `DELETE` run inside a read-only transaction, so a role with only
  `SELECT` can change data through Cypher (graphos's console refuses write clauses itself, but a
  direct SQL reader's role is not safe);
- a variable-length pattern run directly can wedge a database process;
- its btree strategy numbers for agtype `>=` and `>` are swapped.

Every feature since has had to be written and tested against both, and every doc has to say what
differs. The compose file already moves a PostgreSQL 16 stack to 18 by itself on its first start
(a dump and a restore, the old volume kept for going back).

## Decision

- graphos-api **refuses to start** on PostgreSQL before 18 or AGE before 1.8, with an error that
  names the versions found and the move guide. It no longer starts on them with a warning.
- `GET /v1/info` loses `database_deprecated`: no database graphos starts on is deprecated.
- CI's integration job runs on PostgreSQL 18 / AGE 1.8 only.
- **The move stays**: `graphos-db-export` and `graphos-db-import` in the compose file, the scripts
  in `deploy/db-upgrade/` and the upgrade guide, so an install still on 16 gets to 18 with
  `docker compose up -d`. graphos keeps reading what a restored AGE 1.6 dump holds.
- Workarounds for AGE 1.6 that are harmless on 1.8 (the keyset branches that avoid agtype `>=`,
  the console's write-clause guard) stay: they cost nothing and the guard is defence in depth.
- Notes on AGE 1.6's behaviour stay in apache-age.md as history, marked as such.

## Decision options

- **Keep 16 deprecated a while longer.** Nothing breaks now, but every change keeps paying for a
  database that cannot be fixed. Left.
- **Start on 16 with a louder warning.** Leaves the read-only hole open for direct readers with
  no one forced to act. Left.
- **Refuse to start, keep the move.** Taken: an operator who upgrades gets a clear error, and the
  compose fixes it with the command they already run.

## Evidence

- The compose's move from PostgreSQL 16 to 18 is documented step by step, with going back, in
  upgrade-postgresql-18.md. CI does not run it.
- AGE 1.6's behaviours above are reproduced in apache-age.md.

## Consequences

- **Better:** one database to build for, test and document; CI's integration job takes half the
  time; the known limit about direct readers on AGE 1.6 goes away.
- **Worse:** an install on PostgreSQL 16 that upgrades graphos-api without moving its database
  stops at start until it moves. A database run outside the compose must be moved by hand (the
  guide's last part).
- `database_deprecated` disappears from `/v1/info`: a client that reads it must treat it as
  absent (the Python client does not model it).
- The changelog says it under Changed, in bold, with the move.

## Open questions

None.

## Acceptance

- graphos-api fails at start on PostgreSQL 16 / AGE 1.6, AGE 1.7, or no AGE, naming the move guide
  (unit test of the check); it starts on 18 / 1.8.
- CI has no PostgreSQL 16 integration run; SECURITY.md, the README, installation.md, concept.md,
  features.md, usage.md, the service guide and the upgrade guide say only 18 / 1.8 is supported.

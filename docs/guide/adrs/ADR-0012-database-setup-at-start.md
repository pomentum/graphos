# ADR-0012: graphos-api sets its database up at start

- **Status:** Accepted (2026-10-29)
- **Scope:** deployment: graphos-api's start-up, its settings, the release compose file
- **Related:** concept.md §2.2 (schema set-up), installation.md §2 and §10,
  [ADR-0007](ADR-0007-drop-postgresql-16.md), `services/graphos/deploy/db-init/`

## Context

graphos-api connects as `graphos_app`, a role that is not a superuser and cannot create roles. Two
things must exist before it can start, and that role cannot make either:

- the extensions `age` (not a trusted extension: only a superuser creates it), `pg_trgm` and
  `pg_stat_statements`;
- the role `graphos_app` itself, with its password and grants: `CONNECT` and `CREATE` on the
  database, `USAGE` on `ag_catalog` and DML on its tables and sequences, membership in
  `pg_read_all_stats`, and `SET` on `temp_file_limit`.

Until now the scripts in `services/graphos/deploy/db-init/` made them. They run in the database
container: the PostgreSQL image runs `/docker-entrypoint-initdb.d/` once, when it creates an empty
data directory. graphos-api is another image and cannot place files there, so every deployment
mounted the folder into the database container, and a deployment from the published images alone
(a single compose file, a Kubernetes chart) had to copy the scripts into its own configuration.

graphos already brings its own state up to date at every start: `graphos_meta` and its upgrades
(`db.ensure_internal_schema`), the tenants' DDL (`bootstrap.py`). The database set-up is the one
step it could not do.

graphos 0.14.5 is in production on databases the db-init scripts set up. Those must start under
the new version with nothing to do and nothing changed.

## Decision

graphos-api does the db-init set-up itself, at start, before it opens its pool
(`graphos.db_setup`), when it is given a role that may do it:

| Setting | Meaning |
|---|---|
| `GRAPHOS_DB_ADMIN_USER` | A role that may create extensions and roles (the database image's `POSTGRES_USER`). Unset (the default): no set-up, as before. |
| `GRAPHOS_DB_ADMIN_PASSWORD_FILE` / `GRAPHOS_DB_ADMIN_PASSWORD` | Its password, by the `*_FILE` convention of the other secrets. |

At start, with the admin role set:

1. graphos signs in as its own role and reads what is missing: the extensions, and each grant
   checked against the ACL on record for the role (`aclexplode`), not against what the role ends
   up being allowed (the `CONNECT` that `PUBLIC` has is not a grant to graphos's role).
2. When nothing is missing, it is done: it never signs in as the admin.
3. Otherwise it signs in as the admin and, in one transaction under an advisory lock (replicas
   starting together), creates the missing extensions, then the role if it does not exist, then
   the missing grants, each only if it is missing. It logs what it did, by name.
4. It then starts as before, as its own role.

Rules:

- **An existing role is never altered**: not its password, attributes or settings. A role that
  exists and does not take graphos's password gets an error in the log naming the fix
  (`ALTER ROLE ... PASSWORD` by a person), and the start fails at the pool, as before. Rotating the
  password would lock out the role's other clients.
- **A new role's password is sent as a SCRAM-SHA-256 verifier**, computed in graphos (as `psql`'s
  `\password` and `createuser -P` do), never in clear: a server that logs statements, or logs the
  statement of an error, logs no password.
- **The admin password is read when needed and not kept**, and appears in no log. Nothing is
  written anywhere but the database's own catalogue.
- **The set-up never stops a start.** When the admin cannot sign in or a statement fails, the
  transaction rolls back, the error is logged, and graphos starts as it would have without it.
- **graphos creates `graphos_meta`, not the admin**: as its own role, so it owns it, as the db-init
  scripts arranged by handing it over.
- **`pg_trgm` and `pg_stat_statements` go in `public`**, named in the statement, where the db-init
  folder puts them; AGE's schema is fixed (`ag_catalog`). Left to the admin's `search_path` they
  would follow it, and the db-init folder gives the superuser one that starts with `ag_catalog`
  (the DB test's comparison with a db-init role caught this).
- **The admin role itself is not changed.** The db-init folder also set a `search_path` on the
  superuser `graphos`, for operators' own sessions; graphos-api leaves the admin role as it is.

The release compose file sets the admin role (`graphos`, its password file is already mounted)
and **keeps the db-init mount by default**:

- on an existing volume the mount does nothing (the image runs the folder only on an empty data
  directory), and graphos finds nothing missing;
- the move from PostgreSQL 16 (`graphos-db-import`) restores objects owned by `graphos_app` before
  graphos-api starts, so the role must exist by then;
- a new deployment may drop the line: graphos-api then does the set-up on the first start.

## Decision options

| Option | Taken? | Why |
|---|---|---|
| **An optional admin connection used at start, only when something is missing** | Yes | One image does all of it; nothing to mount; no change without the setting; on a set-up database the admin is not even signed in as. |
| Always sign in as the admin when it is set | No | A deployment whose superuser password file is stale or gone (it was "used only by the init scripts") would log an error at every start for nothing. |
| A separate one-shot set-up container (`graphos-api setup`, run before the API) | No | Another service in every deployment, and the same privileges; the in-process step does the same with less. |
| graphos-api connects as the superuser | No | Gives up the unprivileged role: a flaw in the API would then run as the superuser. |
| Ship the db-init scripts in a database image of graphos's own | No | graphos would maintain a PostgreSQL + AGE image; operators with their own database still need the scripts. |
| `GRANT ... ON ALL TABLES` again at every start | No | A re-grant by another grantor adds ACL entries or role memberships (PostgreSQL 16+); "nothing changed" means nothing is sent. |
| Set the existing role's password to graphos's | No | Silent password rotation breaks the role's other clients. |
| Drop the db-init mount from the release compose | No | Breaks the move from PostgreSQL 16, and gains nothing on existing volumes. |

## Evidence

- `tests/test_db_setup_db.py` (DB): on a fresh, empty database with no db-init folder, a graphos
  process with the admin settings creates the extensions and the role, and the role's privileges
  equal those the db-init folder gives `graphos_app` (same attributes, settings, memberships,
  database, schema, table, sequence and parameter grants); the API then signs in, registers a
  tenant and writes nodes and an edge. A second start changes nothing.
- The same file: the test stack's database, set up by the db-init folder, is compared before and
  after graphos processes started with and without the admin settings, and after the admin pass
  itself: roles with their password hashes, role settings, memberships with grantors, the
  database, schema, table and parameter ACLs, owners and extensions are identical.
- An upgrade from the published 0.14.5 images, with data, to this branch's image kept the data,
  the sign-in and the tenant, and a new stack without the db-init mount came up and served.
- PostgreSQL's documentation on `CREATE ROLE ... PASSWORD`: a password in clear "might also be
  logged in the client's command history or the server log"; an already-encrypted (SCRAM)
  password is stored as given.

## Consequences

- A deployment needs no file in the database container: the database's own settings
  (`shared_preload_libraries=age,pg_stat_statements`) and graphos-api's environment are enough.
- graphos-api holds the superuser's password at start, when configured. The release compose
  already mounted that file into graphos-api's container (the whole secrets folder); graphos reads
  it only when something is missing.
- A restored database (installation.md, Backup and restore) gets its database-level grant from
  graphos-api; the manual `GRANT` stays documented for deployments without the admin setting.
- The db-init folder stays, in step with `graphos.db_setup`: the DB test compares the two.
- Docs: installation §2, §8 (Backup and restore), §9, §10; concept §2.2; usage §1.1; the README's
  configuration table; the upgrade guide's "a database you run yourself".

## Open questions

- Whether the release compose drops the db-init mount once the move from PostgreSQL 16 is retired.

## Acceptance

- Unit tests (`tests/test_db_setup.py`) and the DB tests above pass; ruff and mypy clean.
- Existing deployments: nothing to do; their database is unchanged.

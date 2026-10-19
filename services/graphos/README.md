# graphos

FastAPI service for graphos — a multi-tenant property-graph service backed by PostgreSQL 18 + Apache AGE 1.8 (it does not start on PostgreSQL 16 + AGE 1.6; to move, see [docs/guide/upgrade-postgresql-18.md](../../docs/guide/upgrade-postgresql-18.md)).

This directory is the Python package. For the overview, the docker-compose stack and the quickstart, see the repository [`README.md`](../../README.md).

## Local dev

```bash
python -m venv .venv
source .venv/Scripts/activate    # Windows; on macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"          # or ".[dev,louvain]" to include Louvain community detection
ruff check src tests
pytest -v
```

The `louvain` extra pulls in python-igraph (GPL-2.0-or-later); without it `POST /v1/{tenant}/recompute-communities` answers 501. Tests marked `db` need a live database and are skipped unless `GRAPHOS_TEST_DSN` is set.

Before changing the service, read [DEVELOPMENT.md](DEVELOPMENT.md): where to look, the data-model invariants, the database tests and the conventions.

## Run

Point the service at a PostgreSQL 18 + AGE 1.8 database (it refuses older ones) and give it its credentials:

```bash
GRAPHOS_DB_HOST=localhost \
GRAPHOS_DB_PASSWORD=...  \
python -m graphos
```

- Role: `GRAPHOS_DB_USER` (default `graphos`). The bundled compose uses `graphos_app`, an unprivileged role created by [`deploy/db-init/01-app-role.sh`](deploy/db-init/01-app-role.sh) on first volume init. A non-superuser cannot `LOAD 'age'` (graphos tries and carries on), so the server must have AGE in `shared_preload_libraries`. For the Cypher console's temporary-file limit (`GRAPHOS_CYPHER_TEMP_FILE_LIMIT`), the role needs `GRANT SET ON PARAMETER temp_file_limit` (the init script grants it to `graphos_app`); without it graphos logs a warning and the console runs unbounded.
- Password: `GRAPHOS_DB_PASSWORD`, or `GRAPHOS_DB_PASSWORD_FILE` pointing at a file that holds it (`deploy/secrets/graphos_app_pw.txt` for `graphos_app`).
- Auth: users and grants stored in the database. The first start creates the admin `graphos` with the password `graphos` (or the one in `GRAPHOS_ADMIN_PASSWORD_FILE`); it must be changed at the first sign-in (`POST /v1/me/password`, or the UI). Admins then create people and service accounts and grant them levels on tenants (see [docs/guide/usage.md §2](../../docs/guide/usage.md#2-users-sign-in-and-access)). For local use only, `GRAPHOS_AUTH_DISABLED=true` treats every request as an anonymous admin.

The service binds to `0.0.0.0:8080` by default and trusts `X-Forwarded-*` headers only from `GRAPHOS_FORWARDED_ALLOW_IPS` (default `127.0.0.1`; set it to your reverse proxy). See [`src/graphos/config.py`](src/graphos/config.py) for the full env-var surface (pool size, `GRAPHOS_STATEMENT_TIMEOUT_S` — the Postgres `statement_timeout`, the only per-statement limit — rate limiting, logging). Every write endpoint uses the same COPY-staged SQL upsert ([`src/graphos/writes.py`](src/graphos/writes.py)); there is no switch for it.

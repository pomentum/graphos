# graphos

FastAPI service for graphos — a multi-tenant property-graph service backed by PostgreSQL 16 + Apache AGE.

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

## Run

Point the service at a PostgreSQL 16 + AGE database and give it credentials and an auth mode:

```bash
GRAPHOS_DB_HOST=localhost \
GRAPHOS_DB_PASSWORD=...  \
GRAPHOS_AUTH_TOKENS_FILE=./deploy/secrets/graphos_tokens.json \
python -m graphos
```

- Role: `GRAPHOS_DB_USER` (default `graphos`). The bundled compose uses `graphos_app`, an unprivileged role created by [`deploy/db-init/01-app-role.sh`](deploy/db-init/01-app-role.sh) on first volume init. A non-superuser cannot `LOAD 'age'` (graphos tries and carries on), so the server must have AGE in `shared_preload_libraries`.
- Password: `GRAPHOS_DB_PASSWORD`, or `GRAPHOS_DB_PASSWORD_FILE` pointing at a file that holds it (`deploy/secrets/graphos_app_pw.txt` for `graphos_app`).
- Auth: `GRAPHOS_AUTH_TOKENS_FILE` (JSON object of token → tenants / admin; malformed entries are skipped with an error log). For local use only, `GRAPHOS_AUTH_DISABLED=true` instead treats every request as an anonymous admin. graphos refuses to start with neither.

The service binds to `0.0.0.0:8080` by default and trusts `X-Forwarded-*` headers only from `GRAPHOS_FORWARDED_ALLOW_IPS` (default `127.0.0.1`; set it to your reverse proxy). See [`src/graphos/config.py`](src/graphos/config.py) for the full env-var surface (pool size, `GRAPHOS_STATEMENT_TIMEOUT_S` — the Postgres `statement_timeout`, the only per-statement limit — rate limiting, logging). Every write endpoint uses the same COPY-staged SQL upsert ([`src/graphos/cypher.py`](src/graphos/cypher.py)); there is no switch for it.

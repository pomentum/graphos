# graphos

A multi-tenant property-graph service: **PostgreSQL + [Apache AGE](https://age.apache.org/) behind a typed HTTP API.**

Your services send JSON over HTTP. graphos owns the database, writes the Cypher and SQL, enforces tenant isolation, and gives you bulk loading, traversal, aggregation and (optionally) Louvain community detection. Consumers never get a connection string and never send raw Cypher.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/architecture-dark.svg">
  <img src="docs/images/architecture-light.svg" width="100%" alt="Consumers call graphos-api over the graphos-net network with a bearer token. graphos-api checks tenant scope, validates typed contracts, builds Cypher and SQL internally, times out bad queries and emits metrics and audit logs. Only graphos-api reaches PostgreSQL 16 with Apache AGE on an internal network; each tenant is its own AGE graph.">
</picture>

**Why:** see the [whitepaper](docs/whitepaper.html) for the problem graphos solves and what it gives you.

## Features

- **Tenants are isolated AGE graphs.** Each consumer registers its own labels; tokens are scoped to tenants (wrong tenant → `403`, unknown → `404`).
- **Schema-agnostic.** No built-in labels or property names. Every endpoint takes label / property / edge names as parameters.
- **Fast, idempotent writes:** small batches, NDJSON streaming bulk and single-file snapshot import all go through COPY-staged SQL upserts (`INSERT … ON CONFLICT`); in our measurements this was 37–156× faster than per-row Cypher `MERGE` (see [AGE notes](docs/apache-age.md)).
- **Composable reads:** search, neighbors, multi-hop expand, induced subgraph, paginated node scan, stratified sampling, graphology export (sigma.js / Gephi).
- **Graph analytics:** label histograms, top-K by degree, node/edge aggregates, and (optional extra) Louvain communities with IDs that stay stable across runs.
- **Rule-based edge derivation:** declare a property-match rule once at registration; `edges:resolve` writes the edges server-side in one SQL statement.
- **Safe by default:** bearer tokens required (the service refuses to start without them unless told otherwise), every query under a server-side `statement_timeout` (504 when exceeded), registration DDL validated, the database on a network consumers cannot reach.
- **Operable by people who don't know your domain:** Prometheus metrics, JSON logs with W3C `traceparent`, write audit log naming the caller, per-token rate limiting, token reload on `SIGHUP`.

## Quickstart

Requires Docker with Compose v2 and about 4 GB of memory available to Docker (the defaults are laptop-sized; [tuning](docs/apache-age.md) covers larger graphs).

```bash
# 1. Create local secrets: random database passwords and one random admin token
bash scripts/init-secrets.sh          # or: pwsh scripts/init-secrets.ps1
export ADMIN_TOKEN="$(cat services/graphos/deploy/secrets/admin_token.txt)"

# 2. Start graphos-db (Postgres 16 + AGE 1.6) and graphos-api
docker compose -f docker-compose.graphos.yml up -d

# 3. Check it is up
curl -s http://127.0.0.1:8080/readyz
```

Tokens live in `services/graphos/deploy/secrets/graphos_tokens.json` (gitignored). Add one entry per consumer, scoped to its tenants (`{"<token>": {"consumer": "app", "tenants": ["acme"], "admin": false}}`), then `docker kill -s HUP graphos-api` to reload without a restart. The examples below use the admin token for brevity.

**Register a tenant** (idempotent — safe to call on every consumer start):

```bash
curl -s -X POST http://127.0.0.1:8080/v1/tenants \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "content-type: application/json" \
  -d '{"name": "acme",
       "vertex_labels": ["User", "Document"],
       "edge_labels":   ["AUTHORED", "FOLLOWS"]}'
```

**Write nodes, then edges** (upsert on your `id`; property updates are additive):

```bash
curl -s -X POST http://127.0.0.1:8080/v1/acme/graph/nodes \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "content-type: application/json" \
  -d '{"nodes": [
        {"id": "u1", "label": "User",     "properties": {"name": "ada"}},
        {"id": "d1", "label": "Document", "properties": {"name": "graphs-101"}}]}'

curl -s -X POST http://127.0.0.1:8080/v1/acme/graph/edges \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "content-type: application/json" \
  -d '{"edges": [{"src_id": "u1", "src_label": "User",
                  "dst_id": "d1", "dst_label": "Document", "type": "AUTHORED"}]}'
```

**Read** (node hits carry AGE's internal `id`; the id you wrote is `properties.id`):

```bash
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "http://127.0.0.1:8080/v1/acme/graph/search?q=ada"
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "http://127.0.0.1:8080/v1/acme/graph/neighbors?node_id=u1"
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "http://127.0.0.1:8080/v1/acme/graph/expand?node_id=u1&max_hops=2"
```

**Compute communities** (admin token; writes `community_id` onto every node; needs the `louvain` extra, which the compose build includes):

```bash
curl -s -X POST http://127.0.0.1:8080/v1/acme/recompute-communities \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "content-type: application/json" -d '{}'
```

The full, live API contract is at `http://127.0.0.1:8080/docs` (OpenAPI: `/openapi.json`).

### Louvain is an optional extra

Community detection uses [python-igraph](https://python.igraph.org/), which is GPL-2.0-or-later, so it is kept out of the default install:

| Install | Louvain |
|---|---|
| `pip install ./services/graphos` / default image | no; `recompute-communities` answers `501` |
| `pip install "./services/graphos[louvain]"` | yes |
| `docker build --build-arg GRAPHOS_EXTRAS=louvain` | yes (the image then contains GPL code) |
| `docker compose ... up` (local build) | yes by default; `GRAPHOS_EXTRAS= docker compose ...` to leave it out |

`GET /v1/info` reports `features.louvain` so clients can check before calling.

## Container image

The [`graphos-image`](.github/workflows/image.yml) workflow builds and smoke-tests both variants (`linux/amd64`) on every pull request and push, and publishes the default (permissive-only) variant for `linux/amd64` and `linux/arm64` to `ghcr.io/<owner>/graphos-api` from `main` and `v*` tags. The `louvain` variant is built and smoke-tested but not published.

## Python client

```bash
pip install -e clients/python
```

```python
from graphos_client import GraphosClient, NodeUpsert

async with GraphosClient(base_url="http://127.0.0.1:8080", token=ADMIN_TOKEN, tenant="acme") as cli:
    await cli.upsert_nodes([NodeUpsert(id="u2", label="User", properties={"name": "grace"})])
    hits = await cli.search("grace", limit=10)
```

Async, typed (Pydantic), with pooled connections and safe retries. `GraphosPool` manages one registered client per tenant for services that write to many tenants. See [`clients/python/`](clients/python/) and its [runnable examples](clients/python/examples/).

## Configuration

All settings are environment variables prefixed `GRAPHOS_` (full list in [`config.py`](services/graphos/src/graphos/config.py)). The ones you are most likely to touch:

| Variable | Default | Purpose |
|---|---|---|
| `GRAPHOS_DB_PASSWORD_FILE` | — | Database password (or `GRAPHOS_DB_PASSWORD`) |
| `GRAPHOS_AUTH_TOKENS_FILE` | — (required) | JSON file of tokens → tenants / admin |
| `GRAPHOS_AUTH_DISABLED` | `false` | `true` makes every request an anonymous admin: local development only |
| `GRAPHOS_STATEMENT_TIMEOUT_S` | `60` | Per-statement limit; exceeded → `504` problem+json |
| `GRAPHOS_RATE_LIMIT_ENABLED` | `false` | Per-token sliding-window rate limit |
| `GRAPHOS_DB_USER` | `graphos` (compose: `graphos_app`) | Database role; compose uses an unprivileged role created at init |
| `GRAPHOS_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted |
| `GRAPHOS_DB_POOL_MAX_SIZE` | `16` | asyncpg pool size per replica |

## Documentation

| Doc | Read it for |
|---|---|
| [Whitepaper](docs/whitepaper.html) | The problem, the approach, the benefits |
| [Usage guide](docs/usage.md) | Practical integration patterns |
| [Feature survey](docs/features.md) | Every capability, briefly |
| [Concept](docs/concept.md) | Architecture, endpoint catalogue, capacity planning |
| [Apache AGE notes](docs/apache-age.md) | AGE 1.6 pitfalls, indexing, Postgres tuning, measured numbers |

## Built on Apache AGE

graphos stores every tenant as an [Apache AGE](https://age.apache.org/) graph inside PostgreSQL 16 (image `apache/age:release_PG16_1.6.0`). AGE provides the openCypher engine and the per-label storage tables; graphos adds the HTTP boundary, tenancy, bulk paths, indexing and analytics on top. Apache AGE is a project of the Apache Software Foundation, licensed under Apache 2.0, and is not affiliated with graphos.

## Development

```bash
pip install -e "services/graphos[dev,louvain]" -e "clients/python[dev]"
pytest -m "not db" services/graphos/tests      # unit tests, no database
pytest clients/python/tests
bash scripts/run-db-tests.sh                   # integration tests against the compose stack
node tools/docs/diagrams.mjs                    # regenerate docs/images/*.svg
```

`run-db-tests.sh` starts the same compose project (`graphos-db`, volume `graphos_data`), and `--down-volumes` deletes that volume: do not run it on a machine where that stack holds data you want to keep.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for Apache AGE and other dependencies.

# graphos-client

[![PyPI](https://img.shields.io/pypi/v/graphos-client)](https://pypi.org/project/graphos-client/)
[![Python](https://img.shields.io/pypi/pyversions/graphos-client)](https://pypi.org/project/graphos-client/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/pomentum/graphos/blob/main/clients/python/LICENSE)

The async Python client for [graphos](https://github.com/pomentum/graphos), a multi-tenant graph
service built on PostgreSQL and Apache AGE.

- **Typed:** request and response models for every call, and `py.typed`.
- **Async:** `asyncio` and one pooled `httpx` connection pool per client.
- **Many tenants:** `GraphosPool` gives each tenant its own client and registers it on first use.
- **Safe to retry:** network errors and 5xx answers are retried; long operations never run twice.

```bash
pip install graphos-client
```

Python 3.11 or newer (tested on 3.11, 3.12 and 3.13).

## Quickstart

```python
import asyncio
import os

from graphos_client import EdgeUpsert, GraphosClient, NodeUpsert, PasswordAuth


async def main() -> None:
    async with GraphosClient(
        base_url="http://127.0.0.1:8080",
        # A service account allowed to create the tenant (create_tenants), or an admin.
        # Without auth=, the client signs in as GRAPHOS_USER / GRAPHOS_PASSWORD.
        auth=PasswordAuth("loader", os.environ["GRAPHOS_PASSWORD"]),
        tenant="acme",
    ) as g:
        await g.register_tenant(vertex_labels=["User", "Document"], edge_labels=["AUTHORED"])

        await g.upsert_nodes([
            NodeUpsert(id="u1", label="User", properties={"name": "ada"}),
            NodeUpsert(id="d1", label="Document", properties={"name": "graphs-101"}),
        ])
        await g.upsert_edges([
            EdgeUpsert(src_id="u1", src_label="User", dst_id="d1",
                       dst_label="Document", type="AUTHORED"),
        ])

        hits = await g.search(q="ada")                # hit.properties["id"] is your id
        around = await g.neighbors(node_id="u1")
        reach = await g.expand(node_id="u1", max_hops=2)
        print(len(hits.nodes), len(around.neighbors), len(reach.paths))


asyncio.run(main())
```

## Signing in

| Use | When |
|---|---|
| `auth=PasswordAuth(user, secret)` | A graphos user or service account. Signs in and renews the 15-minute token by itself. |
| no `auth=` | The same, with `GRAPHOS_USER` and `GRAPHOS_PASSWORD` from the environment. |
| `auth=BearerAuth(source)` | A program that holds credentials at graphos's identity provider. `source` is an async callable answering `(access token, seconds valid)`; it is called again before expiry and once on a `401`. |

## Which server version

The client and the graphos server are released together, under one version.

- Use the client of your server's version: `pip install "graphos-client==<version>"`.
  `await client.info()` tells the server's.
- A newer client reads an older server's answers: what that server does not send is left at its
  default (`None`, or empty). A call to an endpoint it does not have raises `GraphosError` 404.
- An older client ignores the fields a newer server adds.

What changed in each version: the [changelog](https://github.com/pomentum/graphos/blob/main/CHANGELOG.md).

## Many tenants: `GraphosPool`

For a service that writes to one tenant per customer, project or team. Each tenant gets its own
client and is registered with the shared schema on first use. If the server no longer knows a
label, the tenant is registered again and the call retried once.

```python
from graphos_client import GraphosPool, PasswordAuth, TenantSchema

schema = TenantSchema(
    vertex_labels=["User", "Document"],
    edge_labels=["AUTHORED"],
    property_indexes=[{"label": "Document", "properties": ["kind"]}],   # serves `where kind = ...`
)

async with GraphosPool(base_url=URL, auth=PasswordAuth("loader", SECRET), schema=schema) as pool:
    await pool.call("team_red", lambda g: g.upsert_nodes(nodes))    # registers, writes
    cli = await pool.client("team_red")                              # the same client
```

A registration is one plain request by default. `GraphosPool(..., on_drift="warn")` (or `"fail"`)
registers through `ensure_tenant` instead (below): a dry run first, its drift logged.

## A program's schema: `ensure_tenant`

A program that declares its schema in code and registers it at every start can learn what people
changed in the tenant meanwhile (in the web UI, say) instead of undoing it unseen:

```python
await cli.ensure_tenant(vertex_labels=["User", "Document"], edge_labels=["AUTHORED"],
                        on_drift="warn")      # or "fail", or "ignore" (no dry run)
```

### What it logs

It runs the registration as a dry run first and logs the dry run's `drift` on the logger
`graphos_client.drift`, one line per item, once each:

| Level | What |
|---|---|
| INFO | What the tenant has that the call does not declare. A merge keeps it; most programs declare less than the tenant has. |
| WARNING | What a `force_shrink` would remove; entries the call would replace with another value; settings it would change (`strict_endpoints`, the property mode, the community algorithm); labels, edge types or label pairs dropped earlier that the call creates again. |

With `on_drift="fail"` it raises `SchemaDriftError` (its `drift` is the report) and registers
nothing when the call would remove, replace or change something, or bring a removed item back.
What is only not declared never fails it.

### How it registers

- It registers with `If-Match` set to the dry run's `ETag`, so it applies exactly what was
  previewed.
- If the schema changed in between (another replica of the program starting at the same time), it
  runs the dry run once more on the new version, logs it, and registers with that.
- A second change raises `GraphosError` 412 and nothing is registered. So does a tenant someone
  unregistered in between: registering would restore it, undoing that.
- A tenant the dry run found not registered is registered with `If-None-Match: *` (unless the dry
  run said the call restores an unregistered tenant), so one someone else created meanwhile is not
  registered unchecked.
- An `if_match=` you pass pins the version: the second dry run is then refused with 412.

### Schema managers

A tenant can name one **schema manager**. On a managed tenant only the manager changes more than
the styles (`display`).

- Name it on `register_tenant`, `ensure_tenant` or `import_schema`:
  - `schema_manager="user:loader"`: an existing graphos user or service account;
  - `"oidc-sub:<sub>"`: a caller signed in through the identity provider;
  - `"self"` (or `manage_schema=True`): the caller, whatever it signs in with;
  - `None`: release it.
- Setting it needs the `owner` level. A server with authentication disabled refuses it (422).
- Anyone else's write to a managed schema raises `GraphosError` 409 with
  `code == "schema_managed"` and `schema_manager` naming the manager.
- `get_tenant()` and `schema()` show it: `schema_manager` and `schema_manager_since`, `None` when
  there is none.
- After a registration that names a manager, the client checks the answer. A server older than
  the schema manager ignores the field, and the client then raises `SchemaManagerNotApplied`
  (a `GraphosError` with status 0: the rest of the registration was applied, the manager was not).
- An import of more than 5,000 label pairs sends the manager with its last call, so the calls
  before it are not refused.

How the styles sent merge is `display_merge=`:

- `"entries"`: an entry sent replaces the stored one; `None` removes it.
- `"missing_only"`: only the styles the tenant lacks are added. A `force_shrink` keeps the stored
  styles of the labels and edge types it keeps and drops the others.
- Left out: the manager's registrations get `"missing_only"`, so people's styles stay, and anyone
  else's `"entries"`. A manager that wants to correct a style it shipped sends `"entries"`.

## Copying a tenant or a schema

A whole tenant, data and schema:

```python
await cli.export_dataset("acme.ndjson.gz")                   # every node and edge, and the schema
await cli.import_dataset("acme.ndjson.gz", name="acme_copy",  # a new tenant, restored exactly:
                         schema="apply", mode="empty")        # schema from the file, then the data
```

Both stream: the file is never held in memory. The result says whether the file was complete.

The schema only:

```python
doc = await cli.export_schema()                              # the whole schema, a dict
preview = await cli.import_schema(doc, name="acme_copy", dry_run=True)
print(preview["changes"], preview["warnings"])               # what it would change, and cost
await cli.import_schema(doc, name="acme_copy")               # merge into (or create) acme_copy
await cli.import_schema(doc, replace=True)                   # replace this tenant's schema
```

The document is what `GET /v1/tenants/{name}/spec` answers: a registration body, so it can be kept
in git and reviewed as a diff. More than 5,000 declared label pairs are sent over several calls.

## Examples

Runnable scripts in [`examples/`](https://github.com/pomentum/graphos/tree/main/clients/python/examples).
Each reads `GRAPHOS_URL` (default `http://127.0.0.1:8080`) and signs in as `GRAPHOS_USER` /
`GRAPHOS_PASSWORD`: an admin, or a service account with `create_tenants` on the example tenants
(`acme`, `library`, `team_red`, `team_green`, `team_blue`, and `demo` or `GRAPHOS_DEMO_TENANT`),
which makes it their owner. `07_background_jobs.py` also vacuums, which needs an admin.

| Script | Shows |
|---|---|
| [`01_quickstart.py`](https://github.com/pomentum/graphos/blob/main/clients/python/examples/01_quickstart.py) | Register a tenant; write nodes and edges; search, neighbours, expand. |
| [`02_bulk_load.py`](https://github.com/pomentum/graphos/blob/main/clients/python/examples/02_bulk_load.py) | Batched NDJSON bulk writes, and a one-request snapshot import. |
| [`03_explore.py`](https://github.com/pomentum/graphos/blob/main/clients/python/examples/03_explore.py) | Paginated scans, scoped and induced subgraphs, aggregates, top-K, node properties, the cache. |
| [`04_communities.py`](https://github.com/pomentum/graphos/blob/main/clients/python/examples/04_communities.py) | Communities (Louvain, or Leiden with `algorithm="leiden"`): compute, summarise, look up. |
| [`05_tenant_pool.py`](https://github.com/pomentum/graphos/blob/main/clients/python/examples/05_tenant_pool.py) | `GraphosPool` serving several tenants with one schema. |
| [`06_rule_based_edges.py`](https://github.com/pomentum/graphos/blob/main/clients/python/examples/06_rule_based_edges.py) | A property-match rule, and the server writing the edges. |
| [`07_background_jobs.py`](https://github.com/pomentum/graphos/blob/main/clients/python/examples/07_background_jobs.py) | Long operations as background jobs: submit, wait, the one-recompute rule, cancel. |
| [`08_demo_ecosystem.py`](https://github.com/pomentum/graphos/blob/main/clients/python/examples/08_demo_ecosystem.py) | The demo tenant (an invented software ecosystem, about 450 nodes) and every graph algorithm on it: metrics, top nodes by PageRank and betweenness, communities. |
| [`09_better_answers.py`](https://github.com/pomentum/graphos/blob/main/clients/python/examples/09_better_answers.py) | Better answers from the algorithms on the demo tenant: search ranked by PageRank, a walk scoped to a community, the best hit per community, the bridges. |

## What the client covers

| Area | Methods |
|---|---|
| Tenants | `register_tenant`, `ensure_tenant`, `export_schema`, `import_schema`, `remove_edge_endpoint`, `rename_label`, `rename_edge_type`, `rename_property`, `drop_label`, `list_tenants`, `get_tenant`, `unregister_tenant`, `restore_tenant`, `list_unregistered_tenants`, `me`, `schema`, `schema_summary` |
| Writes | `upsert_nodes`, `upsert_edges`, `bulk_upsert_nodes`, `bulk_upsert_edges`, `import_csv_nodes`, `import_csv_edges`, `snapshot_import`, `upsert_node_properties`, `patch_node`, `patch_edge`, `delete_node`, `delete_nodes`, `delete_edge`, `delete_edges`, `delete_by_property`, `reconcile_node_edges`, `reconcile_type_edges`, `resolve_edges` |
| Reads | `search`, `get_node`, `get_edge`, `check_edges`, `find_paths`, `neighbors`, `expand`, `induced_edges`, `scoped_subgraph`, `sample_subgraph`, `scan_nodes`, `scan_nodes_iter`, `scan_edges`, `scan_edges_iter`, `export_graph` |
| Aggregates | `graph_map`, `map_hubs`, `label_top_by_edge`, `aggregate_nodes`, `aggregate_edges`, `tenant_stats` |
| Cypher console | `cypher`, `saved_queries`, `save_query`, `run_saved_query`, `saved_query`, `delete_saved_query` |
| Dataset | `export_dataset`, `import_dataset`; the function `read_dataset_header` (a file's header, without a server) |
| Communities | `recompute_communities`, `community_map`, `list_communities`, `community_summary`, `community_ids` |
| Graph metrics | `compute_metrics` |
| Property definitions | `property_definitions`, `check_properties` |
| Rule-based edges | `resolve_edges`, `catalogue` |
| Cache | `cache_put`, `cache_get`, `cache_delete` |
| Change feed | `changes`, `changes_head`, `iter_changes`, `stream_changes`, `changes_history` |
| Background jobs | `submit_recompute_communities`, `submit_compute_metrics`, `submit_resolve_edges`, `submit_check_properties`, `submit_vacuum`, `get_job`, `list_jobs`, `cancel_job`, `wait_for_job` |
| Operations | `healthz`, `readyz`, `info` (no token: versions and features); for admins `db_stats`, `slow_queries`, `vacuum` |

Worth knowing about some of them:

- **Tenants.** `register_tenant` takes `edge_endpoints`, `strict_endpoints`, `display`,
  `property_schema`, `deprecated`, `schema_manager` / `manage_schema`, `display_merge`,
  `force_shrink` and `dry_run`, and its answer carries the schema's `etag`. `rename_property` runs
  as a job: `await cli.wait_for_job(result.job_id)`. `drop_label` drops a deprecated, empty label.
  `unregister_tenant(drop_data=True)` deletes the tenant with its data. `schema_summary` is the
  schema as a language model needs it, with the values.
- **Writes.** `delete_nodes` takes a batch of `NodeRef`, `delete_edges` one of
  `EdgeCheckItem` (the end nodes stay). `reconcile_node_edges` and
  `reconcile_type_edges` make a scope exactly a given set of edges; `resolve_edges(mode="reconcile")`
  does the same for the edges a rule derives.
- **Reads.** `check_edges` checks many edges at once, by `EdgeCheckItem`. The `*_iter` scans page
  for you.
- **Aggregates.** `aggregate_nodes(missing=True)` counts nodes without the property under `""`;
  `total` and `distinct` count nodes and values before `limit` (`None` from older servers).
  `tenant_stats` says when metrics and communities were computed (`analyses`) and whether they are
  `stale`.
- **Cypher console** (on unless the server turns it off). `cypher` is read-only and takes `$name`
  parameters; write clauses and functions that are not Cypher's are refused with 400.
- **Dataset.** `export_dataset` streams to a file, gzipped by its suffix; `import_dataset` streams
  from one (`mode="empty"` to restore, `schema="apply"` to register the file's schema first).
- **Communities.** `recompute_communities` takes `algorithm="louvain"` or `"leiden"` (the
  tenant's when omitted); `from_scratch=True` makes Leiden ignore the stored groups.
- **Graph metrics.** `compute_metrics` stores degree, PageRank, components and betweenness as
  `graphos_*` node properties.
- **Property definitions.** `property_definitions` lists every label's and type's definitions and
  mode; `check_properties` (`maintain`) checks the stored rows against them and writes nothing.
- **Change feed.** `stream_changes` follows the feed live and reconnects with `Last-Event-ID`;
  `changes_history` reads the past, newest first.
- **Background jobs.** Each `submit_*` returns a `Job`.

Not wrapped (use HTTP, see the [usage guide](https://github.com/pomentum/graphos/blob/main/docs/guide/usage.md)):
users, grants and sign-in (`/v1/users`, `/v1/grants`, `/v1/auth/*`, `/v1/me/password`),
`PATCH /v1/me/preferences`, and the MCP endpoint `/mcp`, which is for agents (see the
[agents guide](https://github.com/pomentum/graphos/blob/main/docs/guide/agents.md)).

## Behaviour

### Errors

A non-2xx answer raises `GraphosError`, with `status_code`, `detail`, and from a JSON body `body`,
`code` and `schema_manager`.

| Raised | When |
|---|---|
| `GraphosNotReady` (a `GraphosError`) | A `503`: graphos starting, stopping (`code == "draining"`) or without its database, or the database out of memory or connections. |
| `GraphosError` 409, `code == "schema_managed"` | A write refused on a tenant whose schema someone else manages. |
| `GraphosError` 504 / 507 | A statement timeout on the server / the database out of disk space. Both have an `application/problem+json` body and are not retried. |
| `SchemaDriftError` (status 0) | `ensure_tenant(on_drift="fail")`, before registering; its `drift` is the report. |
| `SchemaManagerNotApplied` (status 0) | A registration naming a schema manager the server did not apply, after the registration. |

### Retries

- Network errors and 5xx answers are retried with exponential backoff (`retries`,
  `retry_backoff_s`). A 503's `Retry-After` is waited for when it is longer than the backoff, at
  most 30 s.
- Never retried: `504` timeouts, `507`, read and write timeouts, and 4xx errors (raised at once).
- Never re-run automatically: calls that are long or must not run twice (community recompute,
  graph metrics, rule-based edge resolution, snapshot and dataset import, vacuum, Cypher queries,
  restoring or deleting a tenant with its data), job submissions and cancels.

### A stopping server

graphos drains on SIGTERM
([installation §8](https://github.com/pomentum/graphos/blob/main/docs/guide/installation.md#stopping-graphos-api)):

- Requests answer `503` with `Retry-After: 2`, and are retried as above.
- `stream_changes` reconnects at once after the server's `draining` event, from the last change it
  yielded.
- A job the stop interrupted goes back to the queue under the same id (`Job.attempts` counts it),
  so `wait_for_job` keeps waiting.
- A bulk or CSV upload cut while it was being sent is retried whole, as for any network error;
  upserts make that safe. A snapshot import, never retried, raises; calling it again is safe too.

### Connections and tenants

- One pooled `httpx.AsyncClient` per client: use `async with`, or call `close()`. Pass
  `transport=` to supply your own `httpx` transport (tests, proxies).
- One tenant per client: the tenant is part of every data-plane URL. What a token may touch is
  enforced by the server.

## Development

Working on the client: see [DEVELOPMENT.md](https://github.com/pomentum/graphos/blob/main/clients/python/DEVELOPMENT.md)
in the repository.

## Licence

Apache-2.0. See [LICENSE](https://github.com/pomentum/graphos/blob/main/clients/python/LICENSE) and
[NOTICE](https://github.com/pomentum/graphos/blob/main/clients/python/NOTICE).

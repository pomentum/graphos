# graphos-client

Async Python client for [graphos](https://github.com/pomentum/graphos), a multi-tenant graph service built on PostgreSQL and Apache AGE. Typed request and response models, pooled connections, and a tenant pool for services that write to many tenants.

```bash
pip install graphos-client             # from PyPI, once released
pip install -e clients/python          # from a checkout of the repository
```

Requires Python 3.11+. The client is async-only (`asyncio` + `httpx`).

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

## Many tenants: `GraphosPool`

A service that writes to one tenant per customer, project or team can let the pool manage the clients. Each tenant gets its own client, is registered with the shared schema on first use, and is re-registered (then retried once) if the server no longer knows a label. A registration is one plain request by default; `GraphosPool(..., on_drift="warn")` (or `"fail"`) registers through `ensure_tenant` with that mode instead (below): a dry run first, its drift logged.

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

## A program's schema: `ensure_tenant`

A program that declares its schema in code and registers it on every start can learn what people
changed in the tenant meanwhile (in the web UI, say) instead of undoing it unseen:

```python
await cli.ensure_tenant(vertex_labels=["User", "Document"], edge_labels=["AUTHORED"],
                        on_drift="warn")      # or "fail", or "ignore" (no dry run)
```

It runs the registration as a dry run first and logs the dry run's `drift` on the logger
`graphos_client.drift`, one line per item. At INFO: what the tenant has that the call does not
declare (a merge keeps it; most programs declare less than the tenant has). At WARNING: what a
`force_shrink` would remove, entries the call would replace with a different value, settings it
would change (`strict_endpoints`, the property mode, the community algorithm), and labels, edge
types or label pairs dropped earlier that the call creates again. Then it registers, with
`If-Match` set to the dry run's `ETag`, so it applies exactly what was previewed. If the schema
changed in between (another replica of the program starting at the same time), it runs the dry
run once more on the new version, logs it, and registers with that; a second change raises
`GraphosError` 412 and nothing is registered. An `if_match=` you pass pins the version: the second
dry run is then refused with 412. With `on_drift="fail"` it raises `SchemaDriftError` (its `drift` is the report) and
registers nothing when the call would remove, replace or change something, or bring a removed
item back; what is only not declared never fails it.

A tenant can name one **schema manager**: `schema_manager="user:loader"` (an existing graphos user
or service account), `"oidc-sub:<sub>"` (a caller signed in through the identity provider) or
`"self"` (the caller, whatever it signs in with) on `register_tenant`, `ensure_tenant` or
`import_schema`; `manage_schema=True` is `"self"`; `None` releases it; set by an owner, refused
(422) by a server with authentication disabled. On a managed tenant only the manager changes more
than the styles (`display`): anyone else's such write raises `GraphosError` 409 with
`code == "schema_managed"` and `schema_manager` naming the manager. `get_tenant()` and `schema()`
show it (`schema_manager` and `schema_manager_since`, `None` when there is none).

After a registration that names a manager, the client checks the answer's `schema_manager`: a
server older than the schema manager ignores the field, and the client then raises
`SchemaManagerNotApplied` (a `GraphosError` with status 0: the rest of the registration was
applied, the manager was not). An import of more than 5,000 label pairs sends the manager with
its last call, so the calls before it are not refused.

How the styles sent merge is `display_merge=`: `"entries"` (an entry sent replaces the stored
one, `None` removes it) or `"missing_only"` (only the styles the tenant lacks are added; a
`force_shrink` keeps the stored styles of the labels and edge types it keeps and drops the
others). Left out, the manager's registrations get `"missing_only"`, so people's
styles stay, and anyone else's `"entries"`; a manager that wants to correct a style it shipped
sends `"entries"`.

## Copying a tenant

```python
await cli.export_dataset("acme.ndjson.gz")                   # every node and edge, and the schema
await cli.import_dataset("acme.ndjson.gz", name="acme_copy",  # a new tenant, restored exactly:
                         schema="apply", mode="empty")        # schema from the file, then the data
```

Both stream: the file is never held in memory. The result says whether the file was complete.

## Copying a schema

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

**Signing in.** `auth=PasswordAuth(user, secret)` (or `GRAPHOS_USER` / `GRAPHOS_PASSWORD`) signs in
and renews the 15-minute token by itself. A program that holds credentials at graphos's identity
provider can use `auth=BearerAuth(source)` instead: `source` is an async callable answering
`(access token, seconds valid)`, called again before expiry and once on a `401`.

Runnable scripts in [`examples/`](examples/). Each reads `GRAPHOS_URL` (default `http://127.0.0.1:8080`) and signs in as `GRAPHOS_USER` / `GRAPHOS_PASSWORD`: an admin, or a service account with `create_tenants` on the example tenants (`acme`, `library`, `team_red`, `team_green`, `team_blue`, and `demo` or `GRAPHOS_DEMO_TENANT`), which makes it their owner. `07_background_jobs.py` also vacuums, which needs an admin.

| Script | Shows |
|---|---|
| [`01_quickstart.py`](examples/01_quickstart.py) | register a tenant, write nodes and edges, search / neighbours / expand |
| [`02_bulk_load.py`](examples/02_bulk_load.py) | batched NDJSON bulk writes and a one-request snapshot import |
| [`03_explore.py`](examples/03_explore.py) | paginated scans, scoped and induced subgraphs, aggregates, top-K, node properties, cache |
| [`04_communities.py`](examples/04_communities.py) | communities (Louvain, or Leiden with `algorithm="leiden"`): compute, summarise, look up |
| [`05_tenant_pool.py`](examples/05_tenant_pool.py) | `GraphosPool` serving several tenants with one schema |
| [`06_rule_based_edges.py`](examples/06_rule_based_edges.py) | declaring a property-match rule and letting the server write the edges |
| [`07_background_jobs.py`](examples/07_background_jobs.py) | long operations as background jobs: submit, wait, the one-recompute rule, cancel |
| [`08_demo_ecosystem.py`](examples/08_demo_ecosystem.py) | the demo tenant (an invented software ecosystem, about 450 nodes) and every graph algorithm on it: metrics, the top nodes by PageRank and betweenness, communities |
| [`09_better_answers.py`](examples/09_better_answers.py) | better answers from the algorithms on the demo tenant: search ranked by PageRank, a walk scoped to a community, the best hit per community, the bridges |

## What the client covers

| Area | Methods |
|---|---|
| Tenants | `register_tenant` (with `edge_endpoints`, `strict_endpoints`, `display`, `property_schema`, `deprecated`, `schema_manager` / `manage_schema`, `display_merge`, `force_shrink`, `dry_run`; the answer's `etag`), `ensure_tenant` (a dry run's drift logged, or `SchemaDriftError`, then the registration with its `ETag` as `If-Match`), `export_schema`, `import_schema`, `remove_edge_endpoint`, `rename_label`, `rename_edge_type`, `rename_property` (a job: `wait_for_job(result.job_id)`), `drop_label` (a deprecated, empty one), `list_tenants`, `get_tenant`, `unregister_tenant` (`drop_data=True` deletes it with its data), `restore_tenant`, `list_unregistered_tenants`, `me`, `schema`, `schema_summary` (the schema as a model needs it, with the values) |
| Cypher console (on unless the server turns it off) | `cypher` (read-only, `$name` parameters; write clauses and functions that are not Cypher's are refused with 400), `saved_queries`, `save_query`, `run_saved_query`, `saved_query` (one by name), `delete_saved_query` |
| Dataset | `export_dataset` (streamed to a file, gzip by suffix), `import_dataset` (streamed from one; `mode="empty"` to restore, `schema="apply"` to register the file's schema first), `read_dataset_header` |
| Writes | `upsert_nodes`, `upsert_edges`, `bulk_upsert_nodes`, `bulk_upsert_edges`, `import_csv_nodes`, `import_csv_edges`, `snapshot_import`, `upsert_node_properties`, `patch_node`, `patch_edge`, `delete_node`, `delete_nodes` (a batch, by `NodeRef`), `delete_edge`, `delete_by_property`, `reconcile_node_edges` / `reconcile_type_edges` (a scope made exactly a given set), `resolve_edges` (`mode="reconcile"`) |
| Reads | `search`, `get_node`, `get_edge`, `check_edges` (many edges at once, by `EdgeCheckItem`), `find_paths`, `neighbors`, `expand`, `induced_edges`, `scoped_subgraph`, `sample_subgraph`, `scan_nodes` / `scan_nodes_iter`, `scan_edges` / `scan_edges_iter`, `export_graph` |
| Aggregates | `graph_map`, `map_hubs`, `label_top_by_edge`, `aggregate_nodes` (`missing=True` counts nodes without the property under `""`; `total` and `distinct` count nodes and values before `limit`, `None` from older servers), `aggregate_edges`, `tenant_stats` (`analyses`: when metrics and communities were computed, `stale`) |
| Communities | `recompute_communities` (`algorithm="louvain"` or `"leiden"`, the tenant's when omitted; `from_scratch=True` makes Leiden ignore the stored groups), `community_map`, `list_communities`, `community_summary`, `community_ids` |
| Graph metrics | `compute_metrics` (degree, PageRank, components, betweenness, stored as `graphos_*` node properties) |
| Property definitions | `property_definitions` (every label's and type's definitions and mode), `check_properties` (the stored rows against them, `maintain`; writes nothing) |
| Rule-based edges | `resolve_edges`, `catalogue` |
| Cache | `cache_put`, `cache_get`, `cache_delete` |
| Change feed | `changes`, `changes_head`, `iter_changes`, `stream_changes` (reconnects with `Last-Event-ID`), `changes_history` (newest first, for showing the past) |
| Operations | `healthz`, `readyz`, `info` (no token; versions and features), and for admins `db_stats`, `slow_queries`, `vacuum` |
| Background jobs | `submit_recompute_communities`, `submit_compute_metrics`, `submit_resolve_edges`, `submit_check_properties`, `submit_vacuum` (each returns a `Job`), `get_job`, `list_jobs`, `cancel_job`, `wait_for_job` |

Not wrapped (use HTTP, see the [usage guide](https://github.com/pomentum/graphos/blob/main/docs/guide/usage.md)): users, grants and sign-in (`/v1/users`, `/v1/grants`, `/v1/auth/*`, `/v1/me/password`), `PATCH /v1/me/preferences`, and the MCP endpoint `/mcp`, which is for agents (see the [agents guide](https://github.com/pomentum/graphos/blob/main/docs/guide/agents.md)).

## Behaviour

- **Errors.** Non-2xx responses raise `GraphosError` (`status_code`, `detail`, and `body`, `code` and `schema_manager` from a JSON body: `code == "schema_managed"` is a write refused on a tenant whose schema someone else manages); `ensure_tenant(on_drift="fail")` raises `SchemaDriftError` (`drift`, status 0) before registering; a registration naming a schema manager the server did not apply raises `SchemaManagerNotApplied` (status 0, after the registration); a 503 raises `GraphosNotReady` (graphos starting or without its database, or the database out of memory or connections). A statement timeout on the server is a `504`, the database out of disk space a `507`, both with an `application/problem+json` body and not retried.
- **Retries.** Network errors and 5xx responses are retried with exponential backoff (`retries`, `retry_backoff_s`), except `504` timeouts, `507` (the database out of disk space), read and write timeouts, and calls that are long or should not run twice (community recompute, graph metrics, rule-based edge resolution, snapshot and dataset import, vacuum, Cypher queries, restoring or deleting a tenant with its data), job submissions and cancels, which are never re-run automatically. 4xx errors are raised at once.
- **One tenant per client.** The tenant is part of every data-plane URL; what a token may touch is enforced by the server.
- **Connections.** One pooled `httpx.AsyncClient` per client; use `async with` or call `close()`. Pass `transport=` to supply your own `httpx` transport (tests, proxies).

## Development

Working on the client: see [DEVELOPMENT.md](https://github.com/pomentum/graphos/blob/main/clients/python/DEVELOPMENT.md) in the repository.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

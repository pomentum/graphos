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

A service that writes to one tenant per customer, project or team can let the pool manage the clients. Each tenant gets its own client, is registered with the shared schema on first use, and is re-registered (then retried once) if the server no longer knows a label.

```python
from graphos_client import GraphosPool, TenantSchema

schema = TenantSchema(
    vertex_labels=["User", "Document"],
    edge_labels=["AUTHORED"],
    indexes=['CREATE INDEX IF NOT EXISTS doc_kind ON "{tenant}"."Document" '
             "(((properties::text)::jsonb ->> 'kind'))"],   # {tenant} is substituted
)

async with GraphosPool(base_url=URL, auth=PasswordAuth("loader", SECRET), schema=schema) as pool:
    await pool.call("team_red", lambda g: g.upsert_nodes(nodes))    # registers, writes
    cli = await pool.client("team_red")                              # the same client
```

## Examples

**Signing in.** `auth=PasswordAuth(user, secret)` (or `GRAPHOS_USER` / `GRAPHOS_PASSWORD`) signs in
and renews the 15-minute token by itself. A program that holds credentials at graphos's identity
provider can use `auth=BearerAuth(source)` instead: `source` is an async callable answering
`(access token, seconds valid)`, called again before expiry and once on a `401`.

Runnable scripts in [`examples/`](examples/). Each reads `GRAPHOS_URL` (default `http://127.0.0.1:8080`) and signs in as `GRAPHOS_USER` / `GRAPHOS_PASSWORD` (an admin, or a service account with `create_tenants` on the example tenants and `maintain` for the recompute examples).

| Script | Shows |
|---|---|
| [`01_quickstart.py`](examples/01_quickstart.py) | register a tenant, write nodes and edges, search / neighbours / expand |
| [`02_bulk_load.py`](examples/02_bulk_load.py) | batched NDJSON bulk writes and a one-request snapshot import |
| [`03_explore.py`](examples/03_explore.py) | paginated scans, scoped and induced subgraphs, histograms, top-K, node properties, cache |
| [`04_communities.py`](examples/04_communities.py) | Louvain communities: compute, summarise, look up |
| [`05_tenant_pool.py`](examples/05_tenant_pool.py) | `GraphosPool` serving several tenants with one schema |
| [`06_rule_based_edges.py`](examples/06_rule_based_edges.py) | declaring a property-match rule and letting the server write the edges |
| [`07_background_jobs.py`](examples/07_background_jobs.py) | long operations as background jobs: submit, wait, the one-recompute rule, cancel |
| [`08_demo_ecosystem.py`](examples/08_demo_ecosystem.py) | the demo tenant (an invented software ecosystem, about 450 nodes) and every graph algorithm on it: metrics, the top nodes by PageRank and betweenness, communities |
| [`09_better_answers.py`](examples/09_better_answers.py) | better answers from the algorithms on the demo tenant: search ranked by PageRank, a walk scoped to a community, the best hit per community, the bridges |

## What the client covers

| Area | Methods |
|---|---|
| Tenants | `register_tenant` (with `edge_endpoints`, `strict_endpoints`), `remove_edge_endpoint`, `list_tenants`, `get_tenant`, `unregister_tenant`, `me`, `schema` |
| Writes | `upsert_nodes`, `upsert_edges`, `bulk_upsert_nodes`, `bulk_upsert_edges`, `snapshot_import`, `upsert_node_properties`, `patch_node`, `patch_edge`, `delete_node`, `delete_edge`, `delete_by_property` |
| Reads | `search`, `get_node`, `get_edge`, `find_paths`, `neighbors`, `expand`, `induced_edges`, `scoped_subgraph`, `sample_subgraph`, `scan_nodes` / `scan_nodes_iter`, `scan_edges` / `scan_edges_iter`, `export_graph` |
| Aggregates | `graph_map`, `map_hubs`, `label_histogram`, `label_top_by_edge`, `aggregate_nodes`, `aggregate_edges`, `tenant_stats` |
| Communities | `recompute_communities`, `community_map`, `list_communities`, `community_summary`, `community_ids` |
| Graph metrics | `compute_metrics` (degree, PageRank, components, betweenness, stored as `graphos_*` node properties) |
| Rule-based edges | `resolve_edges`, `catalogue` |
| Cache | `cache_put`, `cache_get`, `cache_delete` |
| Change feed | `changes`, `changes_head`, `iter_changes`, `stream_changes` (reconnects with `Last-Event-ID`), `changes_history` (newest first, for showing the past) |
| Operations (admin) | `healthz`, `readyz`, `info`, `db_stats`, `slow_queries`, `vacuum` |
| Background jobs | `submit_recompute_communities`, `submit_compute_metrics`, `submit_resolve_edges`, `submit_vacuum` (each returns a `Job`), `get_job`, `list_jobs`, `cancel_job`, `wait_for_job` |

## Behaviour

- **Errors.** Non-2xx responses raise `GraphosError` (`status_code`, `detail`); a 503 raises `GraphosNotReady`. A statement timeout on the server is a `504` with an `application/problem+json` body.
- **Retries.** Network errors and 5xx responses are retried with exponential backoff (`retries`, `retry_backoff_s`), except `504` timeouts and long-running operations (Louvain recompute, rule-based edge resolution, snapshot import, vacuum) and job submissions, which are never re-run automatically. 4xx errors are raised at once.
- **One tenant per client.** The tenant is part of every data-plane URL; what a token may touch is enforced by the server.
- **Connections.** One pooled `httpx.AsyncClient` per client; use `async with` or call `close()`. Pass `transport=` to supply your own `httpx` transport (tests, proxies).

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

# graphos-client

Async Python client for [graphos](../../README.md): typed Pydantic models over the graphos REST API.

## Install

```bash
pip install -e clients/python        # from a checkout of this repository
```

## Quickstart

```python
import asyncio
import os

from graphos_client import EdgeUpsert, GraphosClient, NodeUpsert


async def main() -> None:
    async with GraphosClient(
        base_url="http://127.0.0.1:8080",
        token=os.environ["ADMIN_TOKEN"],   # registering a tenant needs an admin token
        tenant="acme",
    ) as cli:
        await cli.register_tenant(
            vertex_labels=["User", "Document"],
            edge_labels=["AUTHORED", "FOLLOWS"],
        )
        await cli.upsert_nodes([
            NodeUpsert(id="u1", label="User", properties={"name": "ada"}),
            NodeUpsert(id="d1", label="Document", properties={"name": "graphs-101"}),
        ])
        await cli.upsert_edges([
            EdgeUpsert(src_id="u1", src_label="User",
                       dst_id="d1", dst_label="Document", type="AUTHORED"),
        ])

        hits = await cli.search(q="ada")               # hit.id is AGE's id; your id is hit.properties["id"]
        around = await cli.neighbors(node_id="u1")
        reach = await cli.expand(node_id="u1", max_hops=2)
        print(len(hits.nodes), len(around.neighbors), len(reach.paths))


asyncio.run(main())
```

## Behaviour

- **HTTP only.** No database credentials and no Cypher in your code.
- **Typed.** Request and response models mirror the server's contracts.
- **Pooled connections** through one `httpx.AsyncClient`; use `async with` or call `close()`.
- **Retries** network errors and 5xx responses with exponential backoff, except `504` statement timeouts and long-running operations (Louvain recompute, rule-based edge resolution), which are never re-run automatically. 4xx errors raise `GraphosError` immediately.
- **One tenant per client**: the tenant is part of every data-plane URL. Token scopes are enforced by the server.

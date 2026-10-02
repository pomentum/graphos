"""Serve many tenants with one schema: GraphosPool.

Each tenant gets its own client and is registered the first time it is used.
``pool.call`` re-registers and retries once if the server no longer knows a
label (for example after the tenant was re-registered elsewhere).

    python clients/python/examples/05_tenant_pool.py
"""

from __future__ import annotations

import asyncio
import os

from graphos_client import EdgeUpsert, GraphosPool, NodeUpsert, TenantSchema

URL = os.environ.get("GRAPHOS_URL", "http://127.0.0.1:8080")
TOKEN = os.environ["GRAPHOS_TOKEN"]          # admin scope: the pool registers tenants

SCHEMA = TenantSchema(
    vertex_labels=["User", "Document"],
    edge_labels=["AUTHORED"],
    # {tenant} is replaced with each tenant's name.
    indexes=[
        'CREATE INDEX IF NOT EXISTS doc_kind ON "{tenant}"."Document" '
        "(((properties::text)::jsonb ->> 'kind'))",
    ],
    meta={"app": "examples"},
)


async def load(pool: GraphosPool, tenant: str) -> int:
    nodes = [NodeUpsert(id="u1", label="User", properties={"name": f"owner of {tenant}"}),
             NodeUpsert(id="d1", label="Document", properties={"name": "welcome", "kind": "guide"})]
    edges = [EdgeUpsert(src_id="u1", src_label="User", dst_id="d1",
                        dst_label="Document", type="AUTHORED")]
    await pool.call(tenant, lambda g: g.upsert_nodes(nodes))
    resp = await pool.call(tenant, lambda g: g.upsert_edges(edges))
    return resp.edges_upserted


async def main() -> None:
    tenants = ["team_red", "team_green", "team_blue"]
    async with GraphosPool(base_url=URL, token=TOKEN, schema=SCHEMA) as pool:
        written = await asyncio.gather(*(load(pool, t) for t in tenants))
        for tenant, n in zip(tenants, written, strict=True):
            hits = await (await pool.client(tenant)).search(q="welcome")
            print(f"{tenant}: {n} edge written, search finds {len(hits.nodes)} document")


if __name__ == "__main__":
    asyncio.run(main())

"""Register a tenant, write a small graph, read it back.

    export GRAPHOS_URL=http://127.0.0.1:8080
    export GRAPHOS_TOKEN="$(cat services/graphos/deploy/secrets/admin_token.txt)"
    python clients/python/examples/01_quickstart.py
"""

from __future__ import annotations

import asyncio
import os

from graphos_client import EdgeUpsert, GraphosClient, NodeUpsert

URL = os.environ.get("GRAPHOS_URL", "http://127.0.0.1:8080")
TOKEN = os.environ["GRAPHOS_TOKEN"]          # admin scope: registration needs it


async def main() -> None:
    async with GraphosClient(base_url=URL, token=TOKEN, tenant="acme") as g:
        # 1. Declare the labels this tenant uses. Idempotent: safe on every start.
        await g.register_tenant(
            vertex_labels=["User", "Document", "Tag"],
            edge_labels=["AUTHORED", "FOLLOWS", "TAGGED_WITH"],
        )

        # 2. Write nodes, then edges. Upserts are keyed on your ids.
        await g.upsert_nodes([
            NodeUpsert(id="u:ada", label="User", properties={"name": "ada"}),
            NodeUpsert(id="u:grace", label="User", properties={"name": "grace"}),
            NodeUpsert(id="d:graphs", label="Document", properties={"name": "graphs-101"}),
            NodeUpsert(id="t:intro", label="Tag", properties={"name": "intro"}),
        ])
        written = await g.upsert_edges([
            EdgeUpsert(src_id="u:ada", src_label="User", dst_id="d:graphs",
                       dst_label="Document", type="AUTHORED", properties={"year": 2026}),
            EdgeUpsert(src_id="u:grace", src_label="User", dst_id="u:ada",
                       dst_label="User", type="FOLLOWS"),
            EdgeUpsert(src_id="d:graphs", src_label="Document", dst_id="t:intro",
                       dst_label="Tag", type="TAGGED_WITH"),
        ])
        print(f"edges written: {written.edges_upserted}, skipped: {written.edges_skipped}")

        # 3. Read. Search hits carry AGE's internal id; your id is properties["id"].
        hits = await g.search(q="ada")
        print("search:", [h.properties["id"] for h in hits.nodes])

        around = await g.neighbors(node_id="u:ada")
        edges = [(n.direction, n.edge_type, n.neighbor_id) for n in around.neighbors]
        print("neighbours of ada:", edges)

        reach = await g.expand(node_id="u:grace", max_hops=3)
        print("grace reaches:", sorted(p.end_id for p in reach.paths))


if __name__ == "__main__":
    asyncio.run(main())

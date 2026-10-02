"""Read patterns that compose: scan, subgraph, aggregates, node properties, cache.

Run 02_bulk_load.py first so there is something to explore.

    python clients/python/examples/03_explore.py
"""

from __future__ import annotations

import asyncio
import os

from graphos_client import GraphosClient

URL = os.environ.get("GRAPHOS_URL", "http://127.0.0.1:8080")
TOKEN = os.environ["GRAPHOS_TOKEN"]


async def main() -> None:
    async with GraphosClient(base_url=URL, token=TOKEN, tenant="acme") as g:
        # Paginated scan with a filter and a projection (cursor handled for you).
        guides = [
            item async for item in g.scan_nodes_iter(
                labels=["Document"],
                where=[{"property": "kind", "op": "=", "value": "guide"}],
                project=["name"],
                page_size=500,
            )
        ]
        print(f"guides: {len(guides)}")

        # Everything about one team: its users, balanced, plus the edges among them.
        team = await g.scoped_subgraph(
            scope_property="team", scope_value="t3", include_properties=["name"], node_cap=200,
        )
        print(f"team t3: {len(team['nodes'])} users, {len(team['edges'])} edges between them")

        # The subgraph induced by any node set you already have.
        ids = [n["id"] for n in team["nodes"][:50]]
        induced = await g.induced_edges(ids, rel_types=["FOLLOWS"])
        print(f"follows among the first 50: {len(induced.edges)}")

        # Aggregates: distribution of a property, and the most-connected nodes.
        hist = await g.label_histogram("Document", property="kind")
        print("documents by kind:", {b.value: b.count for b in hist.bins})
        top = await g.label_top_by_edge("User", by_edge="AUTHORED", direction="out", k=3)
        print("top authors:", [(r.node_id, r.edge_count) for r in top.rows])

        # Stamp computed values onto a node, without knowing its label.
        await g.upsert_node_properties("u0", {"documents_written": top.rows[0].edge_count})
        node = await g.get_node("u0")
        print("u0 now:", node.properties if node else None)

        # A small key/value cache for results with no natural node to live on.
        await g.cache_put("dashboard:kinds", {b.value: b.count for b in hist.bins}, ttl_seconds=600)
        cached = await g.cache_get("dashboard:kinds")
        print("cached:", cached.value if cached else None, "from", cached.source if cached else "-")


if __name__ == "__main__":
    asyncio.run(main())

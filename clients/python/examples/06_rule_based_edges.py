"""Edges from rules: declare a property-match rule once, let the server write the edges.

Documents list the tag keys they carry; a TAGGED edge should exist from each
document to every Tag whose key is in that list. Instead of matching in Python,
register the rule and call ``resolve_edges``: one SQL statement per rule, run
inside the database. Re-running is a no-op for edges that already exist.

    python clients/python/examples/06_rule_based_edges.py
"""

from __future__ import annotations

import asyncio
import os

from graphos_client import GraphosClient, NodeUpsert

URL = os.environ.get("GRAPHOS_URL", "http://127.0.0.1:8080")
# Signs in as GRAPHOS_USER / GRAPHOS_PASSWORD and renews its token by itself.


async def main() -> None:
    async with GraphosClient(base_url=URL, tenant="library") as g:
        await g.register_tenant(
            vertex_labels=["Document", "Tag"],
            edge_labels=[{
                "name": "TAGGED",
                "resolve_rule": {
                    "src_has_array": "tag_keys",     # array property on the source
                    "dst_value_from": "key",         # matched against this target property
                    "require_dst_label": ["Tag"],
                },
            }],
        )
        await g.upsert_nodes([
            NodeUpsert(id="tag:graphs", label="Tag", properties={"key": "graphs"}),
            NodeUpsert(id="tag:sql", label="Tag", properties={"key": "sql"}),
            NodeUpsert(id="doc:1", label="Document", properties={"tag_keys": ["graphs", "sql"]}),
            NodeUpsert(id="doc:2", label="Document", properties={"tag_keys": ["graphs"]}),
        ])

        result = await g.resolve_edges(edge_types=["TAGGED"])
        print("resolve:", result)

        around = await g.neighbors(node_id="doc:1", rel_types=["TAGGED"])
        print("doc:1 is tagged:", sorted(n.neighbor_id for n in around.neighbors))


if __name__ == "__main__":
    asyncio.run(main())

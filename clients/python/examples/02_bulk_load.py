"""Load a larger graph: batched NDJSON bulk writes, then a one-file snapshot.

Both paths are idempotent upserts, so re-running the script is safe.

    python clients/python/examples/02_bulk_load.py
"""

from __future__ import annotations

import asyncio
import os
import random
import time

from graphos_client import EdgeUpsert, GraphosClient, NodeUpsert

URL = os.environ.get("GRAPHOS_URL", "http://127.0.0.1:8080")
# Signs in as GRAPHOS_USER / GRAPHOS_PASSWORD and renews its token by itself.

N_USERS = 2_000
N_DOCS = 5_000
BATCH = 1_000


def batches(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def main() -> None:
    rng = random.Random(7)
    users = [
        NodeUpsert(id=f"u{i}", label="User", properties={"name": f"user-{i}", "team": f"t{i % 12}"})
        for i in range(N_USERS)
    ]
    docs = [
        NodeUpsert(id=f"d{i}", label="Document",
                   properties={"name": f"doc-{i}", "kind": rng.choice(["guide", "note", "spec"])})
        for i in range(N_DOCS)
    ]
    authored = [
        EdgeUpsert(src_id=f"u{rng.randrange(N_USERS)}", src_label="User",
                   dst_id=f"d{i}", dst_label="Document", type="AUTHORED")
        for i in range(N_DOCS)
    ]
    follows = [
        EdgeUpsert(src_id=f"u{i}", src_label="User",
                   dst_id=f"u{rng.randrange(N_USERS)}", dst_label="User", type="FOLLOWS")
        for i in range(N_USERS) for _ in range(3)
    ]

    async with GraphosClient(base_url=URL, tenant="acme") as g:
        await g.register_tenant(
            vertex_labels=["User", "Document", "Tag"],
            edge_labels=["AUTHORED", "FOLLOWS", "TAGGED_WITH"],
        )

        # Nodes first, then edges: an edge whose endpoint does not exist yet is skipped.
        started = time.perf_counter()
        for chunk in batches(users + docs, BATCH):
            await g.bulk_upsert_nodes(chunk)
        written = skipped = 0
        for chunk in batches(authored + follows, BATCH):
            resp = await g.bulk_upsert_edges(chunk)
            written += resp.edges_upserted
            skipped += resp.edges_skipped
        rows = len(users) + len(docs) + len(authored) + len(follows)
        took = time.perf_counter() - started
        print(f"bulk: {rows} rows in {took:.1f}s ({rows / took:,.0f} rows/s); "
              f"{written} edges written, {skipped} skipped")

        # Snapshot import: one request carrying nodes and edges together.
        tags = [NodeUpsert(id=f"t:{k}", label="Tag", properties={"name": k})
                for k in ("intro", "advanced", "reference")]
        tagged = [
            EdgeUpsert(src_id=f"d{i}", src_label="Document", dst_id=tags[i % 3].id,
                       dst_label="Tag", type="TAGGED_WITH")
            for i in range(300)
        ]
        result = await g.snapshot_import(nodes=tags, edges=tagged)
        print(f"snapshot: {result['nodes_upserted']} nodes, {result['edges_upserted']} edges, "
              f"{len(result['errors'])} errors")

        stats = await g.tenant_stats()
        print("tenant stats:", stats.model_dump(exclude={"elapsed_ms"}))


if __name__ == "__main__":
    asyncio.run(main())

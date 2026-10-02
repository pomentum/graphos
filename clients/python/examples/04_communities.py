"""Louvain communities: compute, summarise, look up.

Needs a server built with the optional Louvain extra (the bundled compose file
builds it); otherwise recompute answers 501 and this script says so.

    python clients/python/examples/04_communities.py
"""

from __future__ import annotations

import asyncio
import os
from http import HTTPStatus

from graphos_client import GraphosClient, GraphosError

URL = os.environ.get("GRAPHOS_URL", "http://127.0.0.1:8080")
TOKEN = os.environ["GRAPHOS_TOKEN"]          # recompute needs admin scope


async def main() -> None:
    async with GraphosClient(base_url=URL, token=TOKEN, tenant="acme") as g:
        info = await g.info()
        if not info.get("features", {}).get("louvain"):
            print("this server was built without the Louvain extra; nothing to do")
            return

        try:
            run = await g.recompute_communities(
                rel_types=["FOLLOWS", "AUTHORED"], min_community_size=3,
            )
        except GraphosError as exc:
            if exc.status_code == HTTPStatus.CONFLICT:
                print("a recompute is already running for this tenant; try again later")
                return
            raise
        print(f"{run.n_communities} communities over {run.n_nodes} nodes, "
              f"modularity {run.modularity:.3f}, {run.elapsed_ms} ms")

        summary = await g.community_summary(top=5)
        print("largest:", [(c.community_id, c.size) for c in summary.top])

        # Ids are stable across runs on stable input, so they are safe to store.
        lookup = await g.community_ids(["u0", "u1", "d0", "missing"])
        print("community by id:", lookup.community_by_id)

        # Traversal can be scoped to communities.
        first = summary.top[0].community_id
        print("community", first, "members:", summary.top[0].size)


if __name__ == "__main__":
    asyncio.run(main())

"""Better answers from the graph algorithms: plain reads next to the same reads
ranked, focused and grouped by what Louvain and the graph metrics computed.

Uses the demo tenant: run ``08_demo_ecosystem.py`` first (it computes the
metrics over DEPENDS_ON and, with the Louvain extra, the communities). Every
search hit and neighbour already carries ``graphos_pagerank``,
``graphos_betweenness``, ``graphos_degree`` and ``community_id``: using them
costs no extra request.

    export GRAPHOS_URL=http://127.0.0.1:8080
    export GRAPHOS_USER=graphos GRAPHOS_PASSWORD=...
    python clients/python/examples/09_better_answers.py [query]     # default: flow
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence
from typing import Any

from graphos_client import GraphosClient

URL = os.environ.get("GRAPHOS_URL", "http://127.0.0.1:8080")
TENANT = os.environ.get("GRAPHOS_DEMO_TENANT", "demo")
# How much PageRank weighs against the text match when ranking hits.
IMPORTANCE = 1.0


def metric(properties: dict[str, Any], name: str) -> float:
    value = properties.get(name)
    return float(value) if isinstance(value, int | float) else 0.0


def name_of(properties: dict[str, Any]) -> str:
    return str(properties.get("name") or properties.get("id"))


def show(title: str, rows: Sequence[dict[str, Any]], *columns: str) -> None:
    print(f"\n{title}")
    for i, props in enumerate(rows, 1):
        values = "  ".join(f"{c.removeprefix('graphos_')}={metric(props, c):.4g}" for c in columns)
        print(f"  {i:>2}. {name_of(props):<16} {values}")


async def main(query: str) -> None:
    async with GraphosClient(base_url=URL, tenant=TENANT) as g:
        # The demo's search index, so each hit has a text score to weigh.
        hits = (await g.search(query, index="names", limit=50)).nodes
        if not hits:
            print(f"no node matches {query!r}")
            return

        # 1. Rank: the text match alone, then weighted by importance (PageRank).
        top_pr = max(metric(h.properties, "graphos_pagerank") for h in hits) or 1.0

        def ranked_score(props: dict[str, Any], text_score: float | None) -> float:
            importance = metric(props, "graphos_pagerank") / top_pr
            return (text_score or 1.0) * (1 + IMPORTANCE * importance)

        plain = [h.properties for h in hits[:5]]
        ranked = sorted(hits, key=lambda h: ranked_score(h.properties, h.score), reverse=True)
        show(f"1. search {query!r}: by text score alone ({len(hits)} hits, first 5)", plain,
             "graphos_pagerank")
        show("   ... ranked by importance (PageRank)", [h.properties for h in ranked[:5]],
             "graphos_pagerank")

        best = ranked[0].properties
        group = best.get("community_id")
        if group is None:
            print("\nno communities on this tenant: compute them (Louvain) for steps 2-4")
            return

        # 2. Focus: two hops from the best hit, everywhere, then inside its community.
        everywhere = await g.expand(node_id=str(best["id"]), max_hops=2, limit=500)
        inside = await g.expand(node_id=str(best["id"]), max_hops=2, limit=500,
                                communities=[str(group)])
        reached = {p.end_id: p.end_properties for p in everywhere.paths}
        focused = {p.end_id: p.end_properties for p in inside.paths}
        groups_reached = {props.get("community_id") for props in reached.values()}
        print(f"\n2. two hops from {name_of(best)}: {len(reached)} nodes in "
              f"{len(groups_reached)} communities; inside its own community: {len(focused)} nodes")

        # 3. Diversify: the best hit of each community, named after the community's hub.
        names = {c.community_id: c.hub.properties.get("name") if c.hub else None
                 for c in (await g.community_map()).communities}
        per_group: dict[Any, dict[str, Any]] = {}
        for h in ranked:
            per_group.setdefault(h.properties.get("community_id"), h.properties)
        print("\n3. the best hit of each community (a varied answer, not ten of one kind):")
        for group_id, props in per_group.items():
            label = names.get(group_id) if isinstance(group_id, int) else None
            print(f"     {label or 'no community':<16} {name_of(props)}")

        # 4. Explain: of the nodes reached, the bridges between communities (betweenness).
        bridges = sorted(reached.values(), key=lambda p: metric(p, "graphos_betweenness"),
                         reverse=True)[:5]
        show("4. among the nodes reached, the bridges (highest betweenness)", bridges,
             "graphos_betweenness", "graphos_degree")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "flow"))

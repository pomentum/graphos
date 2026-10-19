"""The community map: the groups Louvain found, and how they connect.

A recompute already holds every edge it clustered in memory
(:class:`graphos.louvain_compute.EdgeArrays`) and knows each node's final
community. One pass over those edges then gives, exactly and for free:

* each community's size and label mix (a node's label is the table its
  graph id names: the top 16 bits),
* its best-connected member (the hub a client can name it after),
* its internal edge count, and the edges between communities per type.

That summary is stored with the recompute by
:func:`graphos.writes.store_community_map` (``graphos_meta.community_map``,
``community_links``, ``community_map_runs``) and read back by
``GET …/graph/communities/map`` without touching the graph. It describes
the graph as of the last recompute, which the response says.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field

from graphos.graph_compute import EdgeArrays

# AGE graph ids carry their label id in the top 16 bits.
_LABEL_SHIFT = 48


@dataclass
class CommunitySummary:
    size: int = 0
    # label id -> members of that label
    labels: Counter[int] = field(default_factory=Counter)
    hub_gid: int | None = None
    hub_degree: int = 0
    internal_edges: int = 0


@dataclass
class CommunityMapData:
    communities: dict[int, CommunitySummary]
    # (source community, target community, relationship type) -> edges
    links: Counter[tuple[int, int, str]]


def summarize(
    edges: EdgeArrays,
    assignments: Mapping[int, int],
    final_of_raw: Mapping[int, int],
) -> CommunityMapData:
    """One pass over the clustered edges: sizes, label mixes, hubs, links.

    ``assignments`` maps graph id to Louvain's raw community, ``final_of_raw``
    raw to the stable id written to the nodes. Edges with an end outside
    every kept community (a singleton, a filtered node) count towards the
    other end's hub degree but not towards any link. CPU-bound: callers run
    it in a worker thread.
    """
    community = {gid: final_of_raw[raw] for gid, raw in assignments.items()}
    summaries: dict[int, CommunitySummary] = {}
    for gid, cid in community.items():
        summary = summaries.get(cid)
        if summary is None:
            summary = summaries[cid] = CommunitySummary()
        summary.size += 1
        summary.labels[gid >> _LABEL_SHIFT] += 1

    degree: Counter[int] = Counter()
    links: Counter[tuple[int, int, str]] = Counter()
    rel_names = edges.rel_names
    for src, dst, rel in zip(edges.src, edges.dst, edges.rel, strict=True):
        degree[src] += 1
        degree[dst] += 1
        cs = community.get(src)
        cd = community.get(dst)
        if cs is None or cd is None:
            continue
        if cs == cd:
            summaries[cs].internal_edges += 1
        else:
            links[(cs, cd, rel_names[rel])] += 1

    for gid, cid in community.items():
        summary = summaries[cid]
        d = degree.get(gid, 0)
        better = summary.hub_gid is None or d > summary.hub_degree
        tie = summary.hub_gid is not None and d == summary.hub_degree and gid < summary.hub_gid
        if better or tie:
            summary.hub_gid, summary.hub_degree = gid, d
    return CommunityMapData(communities=summaries, links=links)

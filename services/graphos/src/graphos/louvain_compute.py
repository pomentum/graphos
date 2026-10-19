"""The Louvain computation, run in a child process per recompute.

Kept apart from :mod:`graphos.louvain` (edge streaming, write-back, stable
ids) so the child process imports only this module and igraph: it starts
in a fraction of a second and holds no service code.

Why a child process per run:

* igraph's Louvain holds Python's GIL for its whole run; in the API process
  that would stall every request and job heartbeat for as long.
* Cancelling a recompute terminates the child, which stops the computation
  and frees its memory at once; the next recompute does not wait behind it.
* Memory is returned to the system after every run.

The child process, and the one-at-a-time rule it shares with graph metrics,
are :mod:`graphos.graph_compute`'s.

The result depends only on the graph and the seed, not on the order the
edges were read in: vertices are numbered by ascending graphid, and
``simplify`` then leaves the edges in canonical order.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from graphos import graph_compute
from graphos.graph_compute import EdgeArrays

# python-igraph is an optional extra (GPL); see graphos.louvain.
try:
    import igraph as ig
except ImportError:  # pragma: no cover - exercised by installs without the extra
    ig = None

IGRAPH_AVAILABLE = ig is not None


@dataclass(frozen=True)
class Partition:
    """What the child process sends back: plain data, no igraph objects."""

    n_nodes: int
    n_singletons: int
    modularity: float
    assignments: dict[int, int]   # graphid -> community id (largest community = 0)
    sizes: dict[int, int]         # community id -> size


def build_graph(edges: EdgeArrays, rel_weights: Mapping[str, float] | None) -> ig.Graph:
    """Undirected, weighted, simplified graph; vertex ``name`` = graphid.

    Self-loops are dropped.  Each edge weighs ``rel_weights[type]`` (default
    1.0); parallel edges between a pair are summed by ``simplify``.
    Vertices are numbered in ascending graphid order, so the graph is the
    same whatever order the edges arrive in.
    """
    weight_of = [float((rel_weights or {}).get(name, 1.0)) for name in edges.rel_names]
    vertices: set[int] = set()
    for src, dst in zip(edges.src, edges.dst, strict=True):
        if src != dst:
            vertices.add(src)
            vertices.add(dst)
    names = sorted(vertices)
    index = {gid: i for i, gid in enumerate(names)}
    del vertices

    pairs: list[tuple[int, int]] = []
    weights: list[float] = []
    for src, dst, code in zip(edges.src, edges.dst, edges.rel, strict=True):
        if src != dst:
            pairs.append((index[src], index[dst]))
            weights.append(weight_of[code])

    g = ig.Graph(n=len(names), edges=pairs, directed=False)
    g.vs["name"] = names
    g.es["weight"] = weights
    g.simplify(combine_edges={"weight": "sum"})
    return g


def assign_and_relabel(
    partition: list[set[int]], vertex_names: list[int], min_size: int,
) -> tuple[dict[int, int], dict[int, int]]:
    """Drop communities smaller than ``min_size``; number the rest by
    descending size (ties by Louvain's order).

    Returns:
        (graphid -> community id, community id -> size)
    """
    sized = [(cid, members) for cid, members in enumerate(partition) if len(members) >= min_size]
    sized.sort(key=lambda p: (-len(p[1]), p[0]))

    assignments: dict[int, int] = {}
    sizes: dict[int, int] = {}
    for new_id, (_old_id, members) in enumerate(sized):
        sizes[new_id] = len(members)
        for vid in members:
            assignments[vertex_names[vid]] = new_id
    return assignments, sizes


def compute(
    edges: EdgeArrays,
    *,
    rel_weights: Mapping[str, float] | None,
    resolution: float,
    seed: int,
    min_community_size: int,
) -> Partition:
    """Build the graph and run Louvain (in this process)."""
    g = build_graph(edges, rel_weights)
    if g.vcount() == 0:
        return Partition(n_nodes=0, n_singletons=0, modularity=0.0, assignments={}, sizes={})
    # community_multilevel takes its randomness from igraph's global RNG.
    ig.set_random_number_generator(random.Random(seed))
    weights = g.es["weight"]
    clusters = g.community_multilevel(weights=weights, resolution=resolution, return_levels=False)
    partition = [set(c) for c in clusters]
    assignments, sizes = assign_and_relabel(partition, g.vs["name"], min_community_size)
    return Partition(
        n_nodes=g.vcount(),
        n_singletons=sum(1 for m in partition if 0 < len(m) < min_community_size),
        modularity=float(g.modularity(clusters, weights=weights)),
        assignments=assignments,
        sizes=sizes,
    )


# ── Child process ────────────────────────────────────────────────────────────


async def compute_in_process(edges: EdgeArrays, **options: Any) -> Partition:
    """:func:`compute` in a child process (:func:`graphos.graph_compute.run_in_child`).
    Cancelling the awaiting task terminates the child."""
    return cast(
        Partition, await graph_compute.run_in_child(compute, edges, options, what="Louvain"),
    )


def terminate_running() -> None:
    """Stop the computation in progress, if any (API shutdown)."""
    graph_compute.terminate_running()


def __getattr__(name: str) -> Any:
    # ``_running`` moved to graphos.graph_compute; kept readable here.
    if name == "_running":
        return graph_compute.running()
    raise AttributeError(name)

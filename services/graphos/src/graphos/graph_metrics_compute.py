"""Graph metrics, computed in a child process (:mod:`graphos.graph_compute`).

The child imports only this module, numpy and rustworkx (Apache-2.0, a
default dependency, unlike igraph). Edges are directed, parallel edges and
self-loops count, and only nodes with at least one edge of the chosen types
take part.

Values are rounded so a re-run on an unchanged graph yields the same numbers,
and the parent writes back only what changed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import rustworkx as rx

from graphos.graph_compute import EdgeArrays

# The properties each metric writes (graphos_* is reserved: graphos.reserved).
PROPERTIES: dict[str, tuple[str, ...]] = {
    "degree": ("graphos_degree", "graphos_in_degree", "graphos_out_degree"),
    "pagerank": ("graphos_pagerank",),
    "components": ("graphos_component",),
    "betweenness": ("graphos_betweenness",),
}

# Significant digits kept for scores: enough to rank, few enough that float
# noise between runs is not a change.
SIGNIFICANT = 6

# Edges are handed to rustworkx in chunks, so no list of every edge exists at once.
_EDGE_CHUNK = 500_000


@dataclass(frozen=True)
class Metrics:
    """What the child sends back: columns aligned with ``gids`` (ascending)."""

    gids: np.ndarray                     # int64 graphids
    columns: dict[str, np.ndarray]       # property -> values (int64 or float64)
    n_edges: int
    # components: raw ids number components by descending size (0 = largest).
    component_sizes: dict[int, int]
    betweenness_skipped: bool


def round_sig(values: np.ndarray, digits: int = SIGNIFICANT) -> np.ndarray:
    """Round to ``digits`` significant digits (zeros stay zero)."""
    out = np.zeros_like(values, dtype=np.float64)
    nonzero = values != 0
    magnitude = np.floor(np.log10(np.abs(values[nonzero])))
    scale = 10.0 ** (digits - 1 - magnitude)
    out[nonzero] = np.round(values[nonzero] * scale) / scale
    return out


def build_graph(edges: EdgeArrays) -> tuple[np.ndarray, np.ndarray, np.ndarray, rx.PyDiGraph]:
    """Node graphids (ascending), edge endpoints as node indexes, and the digraph."""
    src = np.frombuffer(edges.src, dtype=np.int64) if len(edges) else np.zeros(0, np.int64)
    dst = np.frombuffer(edges.dst, dtype=np.int64) if len(edges) else np.zeros(0, np.int64)
    gids = np.unique(np.concatenate([src, dst]))
    s = np.searchsorted(gids, src)
    d = np.searchsorted(gids, dst)
    g = rx.PyDiGraph(multigraph=True)
    g.add_nodes_from(range(len(gids)))
    for start in range(0, len(s), _EDGE_CHUNK):
        g.add_edges_from_no_data(
            list(zip(s[start:start + _EDGE_CHUNK].tolist(), d[start:start + _EDGE_CHUNK].tolist(),
                     strict=True)),
        )
    return gids, s, d, g


def compute(
    edges: EdgeArrays,
    *,
    metrics: list[str],
    damping: float,
    betweenness_max_nodes: int,
) -> Metrics:
    """Compute ``metrics`` over the edges (in this process)."""
    gids, s, d, g = build_graph(edges)
    n = len(gids)
    columns: dict[str, np.ndarray] = {}
    component_sizes: dict[int, int] = {}
    skipped = False

    if "degree" in metrics:
        out_deg = np.bincount(s, minlength=n).astype(np.int64)
        in_deg = np.bincount(d, minlength=n).astype(np.int64)
        columns["graphos_out_degree"] = out_deg
        columns["graphos_in_degree"] = in_deg
        columns["graphos_degree"] = out_deg + in_deg

    if "pagerank" in metrics and n:
        ranks = rx.pagerank(g, alpha=damping)
        values = np.fromiter((ranks[i] for i in range(n)), dtype=np.float64, count=n)
        columns["graphos_pagerank"] = round_sig(values)

    if "components" in metrics and n:
        parts = sorted(rx.weakly_connected_components(g), key=lambda c: (-len(c), min(c)))
        raw = np.empty(n, dtype=np.int64)
        for cid, members in enumerate(parts):
            raw[list(members)] = cid
            component_sizes[cid] = len(members)
        columns["graphos_component"] = raw

    if "betweenness" in metrics and n:
        if n > betweenness_max_nodes:
            skipped = True
        else:
            scores = rx.digraph_betweenness_centrality(g, normalized=True)
            values = np.fromiter((scores[i] for i in range(n)), dtype=np.float64, count=n)
            columns["graphos_betweenness"] = round_sig(values)

    return Metrics(
        gids=gids, columns=columns, n_edges=len(edges),
        component_sizes=component_sizes, betweenness_skipped=skipped,
    )

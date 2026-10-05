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

One computation runs at a time per API process, which bounds memory to one
in-memory graph.

The result depends only on the graph and the seed, not on the order the
edges were read in: vertices are numbered by ascending graphid, and
``simplify`` then leaves the edges in canonical order.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import multiprocessing
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from typing import Any

# python-igraph is an optional extra (GPL); see graphos.louvain.
try:
    import igraph as ig
except ImportError:  # pragma: no cover - exercised by installs without the extra
    ig = None


@dataclass
class EdgeArrays:
    """The tenant's edges as compact columns: ``src[i]``/``dst[i]`` are
    graphids, ``rel[i]`` indexes ``rel_names``.  About 20 bytes per edge,
    against well over 100 for a Python tuple, and cheap to hand to the
    child process."""

    src: array.array = field(default_factory=lambda: array.array("q"))
    dst: array.array = field(default_factory=lambda: array.array("q"))
    rel: array.array = field(default_factory=lambda: array.array("I"))
    rel_names: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.src)


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


def _child(conn: Connection) -> None:
    try:
        edges, options = conn.recv()
        conn.send(("ok", compute(edges, **options)))
    except BaseException as exc:  # noqa: BLE001 — report every failure to the parent
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def _receive(conn: Connection) -> Partition:
    try:
        status, payload = conn.recv()
    except EOFError:
        raise RuntimeError(
            "the Louvain process ended without a result (killed, or out of memory)"
        ) from None
    if status != "ok":
        raise RuntimeError(f"Louvain computation failed: {payload}")
    return payload


_one_at_a_time = asyncio.Lock()
_running: multiprocessing.process.BaseProcess | None = None


async def compute_in_process(edges: EdgeArrays, **options: Any) -> Partition:
    """:func:`compute` in a child process.  Cancelling the awaiting task
    terminates the child."""
    global _running  # noqa: PLW0603 — the one child of this process, for shutdown
    async with _one_at_a_time:
        ctx = multiprocessing.get_context("spawn")
        conn, child_conn = ctx.Pipe()
        proc = ctx.Process(target=_child, args=(child_conn,), daemon=True)
        _running = proc
        # Starting a spawn child and sending it the edges both block until
        # the new interpreter reads them: keep both off the event loop.  The
        # start is shielded so a cancel cannot leave a child starting unseen.
        start = asyncio.ensure_future(asyncio.to_thread(proc.start))
        try:
            await asyncio.shield(start)
            await asyncio.to_thread(conn.send, (edges, options))
            return await asyncio.to_thread(_receive, conn)
        finally:
            with contextlib.suppress(BaseException):
                await start               # cancelled mid-start: let it finish
            child_conn.close()
            if proc.is_alive():
                proc.terminate()          # cancelled, or the result is already in
            if proc.pid is not None:
                await asyncio.to_thread(proc.join, 10)
            conn.close()
            _running = None


def terminate_running() -> None:
    """Stop the computation in progress, if any (API shutdown)."""
    if _running is not None and _running.is_alive():
        _running.terminate()

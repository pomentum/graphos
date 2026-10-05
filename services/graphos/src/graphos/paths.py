"""Shortest paths between two vertices: bidirectional breadth-first search.

The search grows one frontier from each end, a whole level at a time,
always the smaller frontier, until the two meet.  Each level is one batched
call to ``expand`` (in the API, one SQL statement over the edge tables'
``(start_id, end_id)`` and ``(end_id)`` indexes), so a path of n hops costs
n statements whatever the size of the graph; what grows is the number of
edges a level touches, which ``max_edges`` caps.

Every shortest path is found, not just one: a vertex reached by several
edges in the same level keeps all of them as parents, and the paths through
every meeting vertex at the shortest total length are enumerated lazily, up
to ``limit``.

The module knows nothing about SQL or AGE: vertices and edges are graphids
(integers), and ``expand`` is injected.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from itertools import islice
from typing import Literal

Walk = Literal["out", "in", "both"]
_REVERSE: dict[Walk, Walk] = {"out": "in", "in": "out", "both": "both"}


@dataclass(frozen=True, slots=True, order=True)
class Edge:
    gid: int
    type: str
    start: int   # the edge's stored direction, whichever way it was walked
    end: int


@dataclass(frozen=True, slots=True)
class Step:
    """An edge walked from a frontier vertex ``src`` to vertex ``dst``."""

    src: int
    dst: int
    edge: Edge


# ``expand(frontier, walk, cap)``: the edges leaving ``frontier`` in the walk
# direction (``out``: start_id in frontier; ``in``: end_id in frontier), at
# most ``cap + 1`` of them so the caller can tell the cap was reached.
Expand = Callable[[list[int], Walk, int], Awaitable[list[Step]]]


@dataclass(frozen=True, slots=True)
class Path:
    vertices: list[int]
    edges: list[Edge]   # edges[i] joins vertices[i] and vertices[i + 1]


@dataclass(frozen=True, slots=True)
class PathSearch:
    paths: list[Path]
    hops: int | None    # length of the shortest paths; None when none found
    truncated: bool     # a level exceeded max_edges: a path may exist
    visited: int


@dataclass(slots=True)
class _Side:
    root: int
    walk: Walk
    depth: int = 0
    depth_of: dict[int, int] = field(default_factory=dict)
    parents: dict[int, list[tuple[int, Edge]]] = field(default_factory=dict)
    frontier: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.depth_of[self.root] = 0
        self.frontier = {self.root}

    def from_root(self, v: int) -> Iterator[tuple[list[int], list[Edge]]]:
        """Every shortest chain root → ``v``, as (vertices, edges)."""
        if v == self.root:
            yield [v], []
            return
        for prev, edge in self.parents[v]:
            for vertices, edges in self.from_root(prev):
                yield [*vertices, v], [*edges, edge]


async def shortest_paths(  # noqa: PLR0913 — the search's knobs
    src: int,
    dst: int,
    *,
    expand: Expand,
    direction: Walk,
    max_hops: int,
    limit: int,
    max_edges: int,
) -> PathSearch:
    """The shortest paths from ``src`` to ``dst`` of at most ``max_hops``.

    ``direction`` is relative to the path: ``out`` follows edges the way
    they point, ``in`` against it, ``both`` either way.  At most ``limit``
    paths are returned, all of the same, shortest, length.
    """
    if src == dst:
        return PathSearch([Path([src], [])], hops=0, truncated=False, visited=1)
    fwd = _Side(src, direction)
    bwd = _Side(dst, _REVERSE[direction])

    while fwd.depth + bwd.depth < max_hops:
        side, other = (fwd, bwd) if len(fwd.frontier) <= len(bwd.frontier) else (bwd, fwd)
        steps = await expand(sorted(side.frontier), side.walk, max_edges)
        visited = len(fwd.depth_of) + len(bwd.depth_of)
        if len(steps) > max_edges:
            return PathSearch([], hops=None, truncated=True, visited=visited)

        side.depth += 1
        reached: dict[int, list[tuple[int, Edge]]] = {}
        for step in sorted(steps, key=lambda s: (s.dst, s.src, s.edge)):
            if step.dst not in side.depth_of:
                reached.setdefault(step.dst, []).append((step.src, step.edge))
        if not reached:   # this side has nowhere left to go
            return PathSearch([], hops=None, truncated=False, visited=visited)
        for v, parents in reached.items():
            side.depth_of[v] = side.depth
            side.parents[v] = parents
        side.frontier = set(reached)

        meeting = [v for v in side.frontier if v in other.depth_of]
        if meeting:
            hops = min(side.depth + other.depth_of[v] for v in meeting)
            best = sorted(v for v in meeting if side.depth + other.depth_of[v] == hops)
            paths = list(islice(_join(fwd, bwd, best), limit))
            return PathSearch(
                paths, hops=hops, truncated=False,
                visited=len(fwd.depth_of) + len(bwd.depth_of),
            )

    return PathSearch(
        [], hops=None, truncated=False, visited=len(fwd.depth_of) + len(bwd.depth_of),
    )


def _join(fwd: _Side, bwd: _Side, meeting: list[int]) -> Iterator[Path]:
    for m in meeting:
        for f_vertices, f_edges in fwd.from_root(m):
            for b_vertices, b_edges in bwd.from_root(m):
                yield Path(
                    vertices=f_vertices + b_vertices[-2::-1],
                    edges=f_edges + b_edges[::-1],
                )

"""Bidirectional shortest-path search (graphos.paths) on in-memory graphs.

Pure Python: ``expand`` walks an edge list instead of running SQL.  Each
case is checked against a plain breadth-first search over every simple path.
"""

from __future__ import annotations

from collections import deque

import pytest

from graphos import paths
from graphos.paths import Edge, Step, Walk

# (gid, type, start, end)
EDGES = [
    (101, "R", 1, 2), (102, "R", 2, 3), (103, "R", 3, 4),   # 1→2→3→4
    (104, "S", 1, 5), (105, "S", 5, 4),                     # 1→5→4
    (106, "R", 4, 6),                                       # 4→6
    (107, "R", 7, 1),                                       # 7→1 (only reachable "in")
    (108, "S", 6, 6),                                       # self-loop
    (109, "R", 2, 4),                                       # 2→4: 1→2→4 ties 1→5→4
]


def _expander(edges: list[tuple[int, str, int, int]], calls: list[int] | None = None):  # noqa: ANN202
    async def expand(frontier: list[int], walk: Walk, cap: int) -> list[Step]:
        if calls is not None:
            calls.append(len(frontier))
        out = []
        for gid, t, s, e in edges:
            edge = Edge(gid, t, s, e)
            if walk in ("out", "both") and s in frontier:
                out.append(Step(s, e, edge))
            if walk in ("in", "both") and e in frontier:
                out.append(Step(e, s, edge))
        return out[: cap + 1]
    return expand


def _reference(edges, src, dst, direction, max_hops) -> tuple[int | None, set]:  # noqa: ANN001
    """Every shortest path by brute force: (hops, {(vertices, edge gids)})."""
    adj: dict[int, list[tuple[int, int]]] = {}
    for gid, _, s, e in edges:
        if direction in ("out", "both"):
            adj.setdefault(s, []).append((e, gid))
        if direction in ("in", "both"):
            adj.setdefault(e, []).append((s, gid))
    found: set = set()
    best = None
    queue = deque([((src,), ())])
    while queue:
        vs, es = queue.popleft()
        if best is not None and len(es) > best:
            break
        if vs[-1] == dst:
            best = len(es)
            found.add((vs, es))
            continue
        if len(es) == max_hops:
            continue
        for nxt, gid in adj.get(vs[-1], []):
            if nxt not in vs:
                queue.append(((*vs, nxt), (*es, gid)))
    return best, found


async def _search(src, dst, *, direction: Walk = "out", max_hops=5, limit=100, max_edges=1000,
                  edges=EDGES) -> paths.PathSearch:  # noqa: ANN001
    return await paths.shortest_paths(
        src, dst, expand=_expander(edges), direction=direction,
        max_hops=max_hops, limit=limit, max_edges=max_edges,
    )


def _as_set(result: paths.PathSearch) -> set:
    return {(tuple(p.vertices), tuple(e.gid for e in p.edges)) for p in result.paths}


@pytest.mark.asyncio
@pytest.mark.parametrize(("src", "dst", "direction", "max_hops"), [
    (1, 4, "out", 5), (1, 6, "out", 5), (4, 1, "out", 5), (4, 1, "in", 5),
    (4, 1, "both", 5), (7, 6, "out", 5), (6, 7, "both", 5), (1, 6, "out", 2),
    (3, 5, "both", 5), (1, 3, "both", 1),
])
async def test_matches_brute_force(src: int, dst: int, direction: Walk, max_hops: int) -> None:
    result = await _search(src, dst, direction=direction, max_hops=max_hops)
    hops, expected = _reference(EDGES, src, dst, direction, max_hops)
    assert result.hops == hops
    assert _as_set(result) == expected
    assert all(len(p.edges) == hops for p in result.paths)
    assert not result.truncated


@pytest.mark.asyncio
async def test_all_shortest_paths_and_limit() -> None:
    result = await _search(1, 4)
    assert result.hops == 2
    assert _as_set(result) == {((1, 2, 4), (101, 109)), ((1, 5, 4), (104, 105))}
    assert len((await _search(1, 4, limit=1)).paths) == 1


@pytest.mark.asyncio
async def test_edges_keep_their_stored_direction() -> None:
    result = await _search(4, 1, direction="in")
    (path,) = [p for p in result.paths if p.vertices == [4, 2, 1]]
    assert [(e.start, e.end) for e in path.edges] == [(2, 4), (1, 2)]


@pytest.mark.asyncio
async def test_same_node_and_no_path() -> None:
    same = await _search(3, 3)
    assert (same.hops, [p.vertices for p in same.paths]) == (0, [[3]])
    none = await _search(6, 1)                         # nothing leaves 6 but its loop
    assert (none.hops, none.paths, none.truncated) == (None, [], False)


@pytest.mark.asyncio
async def test_a_level_over_the_cap_stops_the_search() -> None:
    star = [(1000 + i, "R", 1, 10 + i) for i in range(50)] + [(2000, "R", 30, 99)]
    result = await _search(1, 99, edges=star, max_edges=10)
    assert (result.hops, result.paths, result.truncated) == (None, [], True)


@pytest.mark.asyncio
async def test_expands_the_smaller_frontier() -> None:
    # A hub at the start (1 fans out to 50) and a chain to the end: after the
    # first level the backward side, with one vertex, is the one expanded.
    edges = [(1000 + i, "R", 1, 10 + i) for i in range(50)] + [
        (2000, "R", 30, 200), (2001, "R", 200, 201), (2002, "R", 201, 202),
    ]
    calls: list[int] = []
    result = await paths.shortest_paths(
        1, 202, expand=_expander(edges, calls), direction="out",
        max_hops=6, limit=1, max_edges=1000,
    )
    assert result.hops == 4
    assert calls[0] == 1 and max(calls) < 50

"""Louvain computation: compact edge columns in, plain partition out.

Needs the optional ``louvain`` extra (python-igraph); skipped without it.
"""

from __future__ import annotations

import asyncio
import random
import time

import pytest

from graphos import louvain_compute as lc

pytestmark = pytest.mark.skipif(lc.ig is None, reason="louvain extra not installed")

OPTIONS = {"rel_weights": {"BRIDGE": 0.1}, "resolution": 1.0, "seed": 0xC0DE,
           "min_community_size": 3}


def _edges(rows: list[tuple[int, int, int]], rel_names: list[str]) -> lc.EdgeArrays:
    edges = lc.EdgeArrays(rel_names=rel_names)
    for src, dst, rel in rows:
        edges.src.append(src)
        edges.dst.append(dst)
        edges.rel.append(rel)
    return edges


def _two_cliques() -> lc.EdgeArrays:
    """Two 5-node cliques joined by one edge, plus a self-loop to drop."""
    rows = [
        (base + i, base + j, 0)
        for base in (100, 200) for i in range(5) for j in range(i + 1, 5)
    ]
    rows += [(104, 200, 1), (100, 100, 0)]
    return _edges(rows, ["LINK", "BRIDGE"])


def _clustered(n: int, degree: int, seed: int = 3) -> list[tuple[int, int, int]]:
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        for _ in range(degree):
            j = (i // 50) * 50 + rng.randrange(50) if rng.random() < 0.8 else rng.randrange(n)
            rows.append((10_000 + i, 10_000 + j, rng.randrange(2)))
    return rows


def test_compute_finds_the_two_cliques() -> None:
    out = lc.compute(_two_cliques(), **OPTIONS)
    assert out.n_nodes == 10
    assert sorted(out.sizes.values()) == [5, 5]
    groups: dict[int, set[int]] = {}
    for gid, community in out.assignments.items():
        groups.setdefault(community, set()).add(gid)
    assert sorted(map(sorted, groups.values())) == [
        [100, 101, 102, 103, 104], [200, 201, 202, 203, 204],
    ]


def test_partition_does_not_depend_on_edge_order() -> None:
    rows = _clustered(3000, 6)
    expected = lc.compute(_edges(rows, ["LINK", "BRIDGE"]), **OPTIONS)
    for k in range(5):
        shuffled = rows[:]
        random.Random(k).shuffle(shuffled)
        assert lc.compute(_edges(shuffled, ["LINK", "BRIDGE"]), **OPTIONS) == expected


def test_empty_graph() -> None:
    out = lc.compute(_edges([(1, 1, 0)], ["LINK"]), **OPTIONS)
    assert (out.n_nodes, out.assignments, out.sizes) == (0, {}, {})


@pytest.mark.asyncio
async def test_child_process_gives_the_same_partition() -> None:
    assert await lc.compute_in_process(_two_cliques(), **OPTIONS) == lc.compute(
        _two_cliques(), **OPTIONS,
    )


@pytest.mark.asyncio
async def test_a_failure_in_the_child_is_raised() -> None:
    with pytest.raises(RuntimeError, match="Louvain computation failed"):
        await lc.compute_in_process(_two_cliques(), **{**OPTIONS, "resolution": -1.0})


@pytest.mark.asyncio
async def test_cancelling_terminates_the_child() -> None:
    edges = _edges(_clustered(60_000, 10), ["LINK", "BRIDGE"])   # seconds of work
    task = asyncio.create_task(lc.compute_in_process(edges, **OPTIONS))
    deadline = time.monotonic() + 10
    while lc._running is None and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    child = lc._running
    assert child is not None and child.is_alive()
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not child.is_alive()
    assert lc._running is None
    # The next computation does not wait behind the cancelled one.
    started = time.monotonic()
    await lc.compute_in_process(_two_cliques(), **OPTIONS)
    assert time.monotonic() - started < 10


@pytest.mark.asyncio
async def test_cancelling_during_start_leaves_no_child() -> None:
    import multiprocessing

    for delay in (0.0, 0.01, 0.05, 0.2):
        task = asyncio.create_task(lc.compute_in_process(_two_cliques(), **OPTIONS))
        await asyncio.sleep(delay)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert multiprocessing.active_children() == [], delay
        assert lc._running is None

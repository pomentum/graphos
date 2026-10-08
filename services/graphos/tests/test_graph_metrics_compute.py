"""Graph metrics computation (graphos.graph_metrics_compute), no database."""

from __future__ import annotations

import numpy as np
import pytest

from graphos import graph_compute
from graphos import graph_metrics_compute as mc
from graphos.graph_compute import EdgeArrays

ALL = ["degree", "pagerank", "components", "betweenness"]


def _edges(pairs: list[tuple[int, int]]) -> EdgeArrays:
    edges = EdgeArrays(rel_names=["R"])
    for src, dst in pairs:
        edges.src.append(src)
        edges.dst.append(dst)
        edges.rel.append(0)
    return edges


def _by_gid(result: mc.Metrics, name: str) -> dict[int, float]:
    return dict(zip(result.gids.tolist(), result.columns[name].tolist(), strict=True))


# A star around 10 (11, 12, 13 point at it), a chain 20 -> 21 -> 22, a
# parallel edge 11 -> 10 and a self-loop on 22.
GRAPH = [(11, 10), (12, 10), (13, 10), (11, 10), (20, 21), (21, 22), (22, 22)]


def test_degree_counts_parallel_edges_and_self_loops() -> None:
    result = mc.compute(_edges(GRAPH), metrics=["degree"], damping=0.85,
                        betweenness_max_nodes=100)
    assert result.gids.tolist() == [10, 11, 12, 13, 20, 21, 22]
    assert _by_gid(result, "graphos_in_degree")[10] == 4
    assert _by_gid(result, "graphos_out_degree")[11] == 2
    assert _by_gid(result, "graphos_degree")[22] == 3   # one in from 21, the loop both ways
    assert result.columns["graphos_degree"].dtype.kind == "i"
    assert set(result.columns) == set(mc.PROPERTIES["degree"])


def test_pagerank_ranks_the_hub_first_and_is_rounded() -> None:
    result = mc.compute(_edges(GRAPH), metrics=["pagerank"], damping=0.85,
                        betweenness_max_nodes=100)
    ranks = _by_gid(result, "graphos_pagerank")
    assert max(ranks, key=ranks.get) in (10, 22)            # the star's centre or the loop's sink
    assert ranks[10] > ranks[11]
    assert sum(ranks.values()) == pytest.approx(1.0, abs=1e-4)
    for value in ranks.values():
        assert value == float(f"{value:.6g}")


def test_components_are_numbered_largest_first() -> None:
    result = mc.compute(_edges(GRAPH), metrics=["components"], damping=0.85,
                        betweenness_max_nodes=100)
    comp = _by_gid(result, "graphos_component")
    assert {comp[g] for g in (10, 11, 12, 13)} == {0}        # 4 nodes: the largest
    assert {comp[g] for g in (20, 21, 22)} == {1}
    assert result.component_sizes == {0: 4, 1: 3}


def test_betweenness_and_its_size_limit() -> None:
    chain = _edges([(1, 2), (2, 3)])
    result = mc.compute(chain, metrics=["betweenness"], damping=0.85, betweenness_max_nodes=10)
    between = _by_gid(result, "graphos_betweenness")
    assert between[2] > 0 and between[1] == 0 and between[3] == 0
    skipped = mc.compute(chain, metrics=["betweenness"], damping=0.85, betweenness_max_nodes=2)
    assert skipped.betweenness_skipped and "graphos_betweenness" not in skipped.columns


def test_no_edges_gives_no_nodes() -> None:
    result = mc.compute(EdgeArrays(), metrics=ALL, damping=0.85, betweenness_max_nodes=10)
    assert len(result.gids) == 0 and result.n_edges == 0


def test_round_sig() -> None:
    values = np.array([0.000412345678, 123456.789, 0.0, -2.5e-9])
    assert mc.round_sig(values).tolist() == [0.000412346, 123457.0, 0.0, -2.5e-9]


@pytest.mark.asyncio
async def test_runs_in_a_child_process() -> None:
    result = await graph_compute.run_in_child(
        mc.compute, _edges(GRAPH),
        {"metrics": ALL, "damping": 0.85, "betweenness_max_nodes": 100},
        what="graph metrics",
    )
    assert len(result.gids) == 7
    assert set(result.columns) == {n for m in ALL for n in mc.PROPERTIES[m]}
    assert graph_compute.running() is None


@pytest.mark.asyncio
async def test_a_failure_in_the_child_is_raised() -> None:
    with pytest.raises(RuntimeError, match="graph metrics computation failed"):
        await graph_compute.run_in_child(
            mc.compute, _edges(GRAPH),
            {"metrics": ["pagerank"], "damping": 2.0, "betweenness_max_nodes": 100},
            what="graph metrics",
        )

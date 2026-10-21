"""Graph metrics over a tenant's graph, written back onto its nodes.

PageRank, degree, weakly connected components and betweenness, computed with
rustworkx in a child process (:mod:`graphos.graph_metrics_compute`) over the edges
streamed by :func:`graphos.analysis.stream_edges`, and stored as node
properties under the reserved ``graphos_`` prefix (:mod:`graphos.reserved`).

Only what changed is written: values are rounded in the child, compared with
what each node carries, and the nodes that differ get one merge each in a
single transaction (:func:`graphos.writes.set_node_metrics`), so a re-run on an
unchanged graph writes nothing, bumps no revision and records no change.

Scores (PageRank, betweenness) are written only when they moved by more than
``GRAPHOS_METRICS_MIN_CHANGE`` of the stored value (1 % by default): a few new
edges shift every node's PageRank a little (on 250,000 nodes, 1 % more edges
moved 94 % of them by more than 0.1 %, and 4.7 % by more than 1 %), and
without it a re-run after any change rewrote the whole tenant.  The values
stored then stay within that share of the computed ones.  Counts (degrees)
and component ids are exact and always written when they differ.
Component ids are carried over from the previous run (:func:`analysis.greene_match`).
Nodes that carried values but no longer take part lose them.

Callers hold the tenant's whole-graph lock (:func:`analysis.tenant_lock`),
shared with Louvain.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence

import asyncpg
import numpy as np

from graphos import analysis, changes, graph_compute, graph_metrics_compute, writes
from graphos.contracts.graph_metrics import ComputeMetricsRequest, ComputeMetricsResponse
from graphos.db import allow_long_statements

log = logging.getLogger(__name__)

# Above this many nodes, exact betweenness (O(nodes x edges)) is skipped.
# Set from GRAPHOS_BETWEENNESS_MAX_NODES at startup (configure).
_betweenness_max_nodes = 20_000
# A score that moved by less than this share of its stored value is not
# rewritten (GRAPHOS_METRICS_MIN_CHANGE; 0 writes every change).
_min_change = 0.01


def configure(*, betweenness_max_nodes: int, min_change: float = 0.01) -> None:
    global _betweenness_max_nodes, _min_change  # noqa: PLW0603 — settings, set once at startup
    _betweenness_max_nodes = betweenness_max_nodes
    _min_change = min_change


async def _current_values(
    pool: asyncpg.Pool, tenant: str, names: Sequence[str], gids: np.ndarray,
) -> tuple[dict[str, np.ndarray], list[tuple[int, list[str]]], dict[int, int]]:
    """What the nodes carry now, for the names computed.

    Returns the values of the nodes in ``gids`` (aligned, NaN where absent),
    the nodes outside ``gids`` that carry some of the names (to clear), and
    the sizes of the stored components (for stable ids).
    """
    old = {name: np.full(len(gids), np.nan) for name in names}
    outside: list[tuple[int, list[str]]] = []
    old_component_sizes: dict[int, int] = {}
    selects = ", ".join(f"(n.props ->> '{name}') AS \"{name}\"" for name in names)
    # The properties are cast to jsonb once per node: OFFSET 0 keeps the
    # subquery from being flattened, which would repeat the cast per column.
    sql = (
        f"SELECT n.gid, {selects} FROM ("
        f"SELECT (t.id::text)::bigint AS gid, ((t.properties)::text)::jsonb AS props "
        f'FROM "{tenant}"."_ag_label_vertex" t OFFSET 0) n '
        f"WHERE n.props ?| $1::text[]"
    )
    async with pool.acquire() as conn, conn.transaction():
        # Reads every node carrying a metric: longer than the default timeout.
        await allow_long_statements(conn, 600.0)
        async for row in conn.cursor(sql, list(names), prefetch=20_000):
            gid = int(row["gid"])
            i = int(np.searchsorted(gids, gid))
            inside = i < len(gids) and int(gids[i]) == gid
            present = []
            for name in names:
                text = row[name]
                if text is None:
                    continue
                present.append(name)
                if name == "graphos_component":
                    cid = int(float(text))
                    old_component_sizes[cid] = old_component_sizes.get(cid, 0) + 1
                if inside:
                    old[name][i] = float(text)
            if not inside and present:
                outside.append((gid, present))
    return old, outside, old_component_sizes


def _stable_components(
    raw: np.ndarray, old: np.ndarray, sizes: dict[int, int], old_sizes: dict[int, int],
) -> np.ndarray:
    """Component ids carried over from the previous run where they still fit."""
    pairs: dict[tuple[int, int], int] = {}
    known = ~np.isnan(old)
    for new_raw, old_cid in zip(raw[known].tolist(), old[known].astype(np.int64).tolist(),
                                strict=True):
        pairs[(new_raw, old_cid)] = pairs.get((new_raw, old_cid), 0) + 1
    final_of_raw, _stats = analysis.greene_match(
        new_raw_of_gid={}, new_sizes_by_raw=sizes, pair_counts=pairs, old_sizes=old_sizes,
    )
    lookup = np.zeros(max(sizes, default=-1) + 1, dtype=np.int64)
    for new_raw, final in final_of_raw.items():
        lookup[new_raw] = final
    return lookup[raw]


def _changed(new: np.ndarray, old: np.ndarray, min_change: float) -> np.ndarray:
    """Where a value is to be written: absent before, or different; a score
    (float) only when it moved by more than ``min_change`` of the stored value."""
    if new.dtype.kind != "f" or min_change <= 0:
        return ~(old == new)
    with np.errstate(invalid="ignore"):
        moved = np.abs(new - old) > min_change * np.abs(old)
    return np.isnan(old) | moved


def _patches(
    gids: np.ndarray, new: dict[str, np.ndarray], old: dict[str, np.ndarray],
    min_change: float = 0.0,
) -> list[tuple[int, str]]:
    """``(graphid, json of the values to write)`` for every node with one
    (:func:`_changed`)."""
    changed = {name: _changed(values, old[name], min_change) for name, values in new.items()}
    any_change = np.zeros(len(gids), dtype=bool)
    for mask in changed.values():
        any_change |= mask
    out: list[tuple[int, str]] = []
    for i in np.nonzero(any_change)[0].tolist():
        patch = {}
        for name, values in new.items():
            if changed[name][i]:
                value = values[i]
                patch[name] = int(value) if values.dtype.kind == "i" else float(value)
        out.append((int(gids[i]), json.dumps(patch)))
    return out


async def run_metrics(
    pool: asyncpg.Pool, tenant: str, body: ComputeMetricsRequest,
) -> ComputeMetricsResponse:
    """Compute the requested metrics and write back what changed."""
    started = time.perf_counter()
    async with pool.acquire() as conn:
        started_at = await changes.head(conn)
    edges = await analysis.stream_edges(
        pool, tenant=tenant, rel_types=body.rel_types, node_filter=body.node_filter,
    )
    t_edges = time.perf_counter()
    result: graph_metrics_compute.Metrics = await graph_compute.run_in_child(
        graph_metrics_compute.compute, edges,
        {
            "metrics": list(body.metrics),
            "damping": body.damping,
            "betweenness_max_nodes": _betweenness_max_nodes,
        },
        what="graph metrics",
    )
    t_compute = time.perf_counter()

    names = [
        n for m in body.metrics for n in graph_metrics_compute.PROPERTIES[m] if n in result.columns
    ]
    warnings: list[str] = []
    if result.betweenness_skipped:
        warnings.append(
            f"betweenness skipped: {len(result.gids)} nodes is more than "
            f"GRAPHOS_BETWEENNESS_MAX_NODES ({_betweenness_max_nodes})"
        )
    new = dict(result.columns)
    old, outside, old_component_sizes = (
        await _current_values(pool, tenant, names, result.gids) if names else ({}, [], {})
    )
    if "graphos_component" in new:
        new["graphos_component"] = _stable_components(
            new["graphos_component"], old["graphos_component"],
            result.component_sizes, old_component_sizes,
        )
    patches = _patches(result.gids, new, old, _min_change) if names else []
    written = 0
    if not body.dry_run:
        written = await writes.set_node_metrics(
            pool, tenant=tenant, patches=patches, clears=outside, names=names,
        )
        await analysis.vacuum_after_writeback(pool, tenant, "vertex", written)
        # Recorded even when nothing changed: the values are current as of the run.
        scope = {"rel_types": body.rel_types, "node_filter": body.node_filter}
        await analysis.record_run(
            pool, tenant, "metrics", started_at, scope={k: v for k, v in scope.items() if v},
        )
    t_write = time.perf_counter()
    log.info(
        "graph metrics computed",
        extra={
            "tenant": tenant, "metrics": list(body.metrics), "n_nodes": len(result.gids),
            "n_edges": result.n_edges, "changed": len(patches), "cleared": len(outside),
            "written": written,
        },
    )
    return ComputeMetricsResponse(
        tenant=tenant,
        metrics=list(body.metrics),
        properties=names,
        n_nodes=len(result.gids),
        n_edges_scanned=result.n_edges,
        n_components=len(result.component_sizes) if "components" in body.metrics else None,
        n_changed=len(patches),
        n_cleared=len(outside),
        betweenness_skipped=result.betweenness_skipped,
        warnings=warnings,
        edge_stream_ms=int((t_edges - started) * 1000),
        compute_ms=int((t_compute - t_edges) * 1000),
        writeback_ms=int((t_write - t_compute) * 1000),
        elapsed_ms=int((t_write - started) * 1000),
        dry_run=body.dry_run,
    )

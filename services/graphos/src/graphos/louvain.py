"""Louvain community detection over a tenant's graph.

Edges are streamed out of the AGE edge tables with plain SQL (integer
graphids only), handed to python-igraph's ``community_multilevel`` (Louvain
modularity optimisation) in a child process (:mod:`graphos.louvain_compute`),
and the assignments are written back to vertex properties with COPY + one
``UPDATE ... FROM``.

Determinism: the same edge set and seed give the same partition, whatever
order the rows are stored or read in.  Communities are relabelled by
descending size, and Greene matching carries ids over from the previous run,
so ids stay stable across runs, which makes diff-based monitoring (how many
nodes changed community) cheap.

Single-flight: callers acquire a postgres advisory lock keyed on
``hashtextextended(tenant_name, 1)`` before invoking. Two concurrent recomputes
of the same tenant → 409 at the API layer (see api/louvain.py).
"""

from __future__ import annotations

import array
import asyncio
import bisect
import logging
import time as _t
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import asyncpg

from graphos import analysis, changes, community_map, graph_compute, louvain_compute, writes

# python-igraph is an optional extra (``pip install "graphos[louvain]"``)
# because it is GPL-licensed; the rest of graphos is Apache-2.0 and must
# import cleanly without it.  The recompute endpoint checks
# ``LOUVAIN_AVAILABLE`` and answers 501 when the extra is missing.
LOUVAIN_AVAILABLE = louvain_compute.IGRAPH_AVAILABLE
LOUVAIN_INSTALL_HINT = (
    'Louvain community detection is not installed in this build. Install the '
    'optional extra (pip install "graphos[louvain]") or build the image with '
    '--build-arg GRAPHOS_EXTRAS=louvain. python-igraph is GPL-2.0-or-later.'
)

log = logging.getLogger(__name__)

LOUVAIN_SEED = 0xC0DE
MIN_COMMUNITY_SIZE_DEFAULT = 3



@dataclass(frozen=True, slots=True)
class LouvainResult:
    """Outcome of one Louvain recompute."""

    n_nodes: int
    n_edges_scanned: int
    n_communities: int
    n_assigned: int
    n_singletons: int
    largest_community_size: int
    modularity: float
    edge_stream_ms: int
    igraph_ms: int
    writeback_ms: int




async def _writeback_assignments(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    assignments: Mapping[int, int],
    chunk_size: int = 500,  # noqa: ARG001 — kept for caller compat
) -> tuple[int, dict[int, int]]:
    """Write `community_id` back onto each vertex.

    Returns how many nodes were assigned and the map from Louvain's raw
    community ids to the stable ids written (for the community map).

    Bulk COPY-staged UPDATE (:func:`graphos.writes.set_node_communities`),
    the same shape as the node upsert.  Rather than issue
    one ``MATCH … SET`` cypher per vertex (parser + planner + agtype
    binding round-trip × N — measured ~42 nodes/s on a large tenant, 215 s for
    9 k rows), we:

    1. COPY all (graphid, community_id) pairs into a temp staging table.
    2. Issue ONE SQL ``UPDATE`` against the tenant's
       ``_ag_label_vertex`` parent table — Postgres inheritance routes
       the update to whichever child label table owns each row.

    Two memory- and CPU-conscious extensions sit on top of that base
    shape:

    A. **Compact streaming delta.**  The prior community_id values
       are pulled via an asyncpg server-side cursor (``prefetch=20000``)
       so neither the client nor PG materialises the full ~390 k-row
       result set in RAM.  The (graphid, prior_id) pairs are stored
       in ``array.array('q')`` / ``array.array('i')`` columns — 12
       bytes per vertex (vs ~140 B per entry for a ``dict[int,str]``).
       Footprint on a large tenant drops from ~55 MB to ~5 MB.

    B. **Greene-2010 stable IDs.**  Before computing the delta, the
       streaming pass also accumulates ``(new_raw_id, old_id) →
       overlap_count`` — a tiny dict bounded by clusters × fan-out
       (≪ vertex count).  :func:`_greene_match` then maps each raw
       Louvain cluster ID to a stable final community_id by
       inheriting the prior ID of its highest-Jaccard predecessor
       (≥ ``GREENE_JACCARD_THETA``), so an idempotent recompute
       produces an *identical* community_id integer for every
       vertex.  The diff-in-Python step then yields ``delta == 0``
       and the COPY + UPDATE path is skipped entirely.

    DB-load profile: one sequential scan of ``_ag_label_vertex``
    streamed in 20 k-row prefetch batches, plus the existing COPY +
    UPDATE only when the delta is non-empty.  No GROUP BY / JOIN
    server-side; no spike risk.

    Returns the number of vertices in the input ``assignments``
    (regardless of how many actually got written).
    """
    if not assignments:
        return 0, {}

    # New-side aggregates: cluster sizes by RAW (size-sorted) ID,
    # built directly from the in-memory partition.  No DB round-trip.
    new_sizes_by_raw: dict[int, int] = {}
    for raw in assignments.values():
        new_sizes_by_raw[raw] = new_sizes_by_raw.get(raw, 0) + 1

    # Streaming accumulators.  ``array.array`` typecodes:
    #   'q' = signed int64 (graphid)   — 8 B
    #   'i' = signed int32 (community) — 4 B
    # Per-vertex client memory ~12 B vs ~140 B for a dict[int,str].
    gids_arr: array.array[int] = array.array("q")
    olds_arr: array.array[int] = array.array("i")
    pair_counts: dict[tuple[int, int], int] = {}
    old_sizes: dict[int, int] = {}

    # Server-side cursors need a transaction; this one only reads.
    async with pool.acquire() as conn, conn.transaction():
        # Server-side cursor: PG streams rows in prefetch batches
        # rather than materialising the full result on either side.
        # ``int`` cast on the agtype-extracted text keeps the value
        # in a 4-byte int32 on the wire and in ``olds_arr``.
        sql = (
            f'SELECT (t.id::text)::bigint AS gid, '
            f"  ((((t.properties)::text)::jsonb) ->> 'community_id')::int AS cid "
            f'FROM "{tenant}"."_ag_label_vertex" t '
            f"WHERE ((((t.properties)::text)::jsonb) ->> 'community_id') "
            f"      IS NOT NULL"
        )
        async for row in conn.cursor(sql, prefetch=20_000):
            gid = int(row["gid"])
            old = int(row["cid"])
            gids_arr.append(gid)
            olds_arr.append(old)
            old_sizes[old] = old_sizes.get(old, 0) + 1
            new_raw = assignments.get(gid)
            if new_raw is not None:
                key = (new_raw, old)
                pair_counts[key] = pair_counts.get(key, 0) + 1

        # Greene 2010 stable-ID matching — pure function, sub-ms on
        # ~400 clusters.  Returns the remap ``raw → final``.
        final_of_raw, stats = _greene_match(
            new_raw_of_gid=assignments,
            new_sizes_by_raw=new_sizes_by_raw,
            pair_counts=pair_counts,
            old_sizes=old_sizes,
        )
        log.info(
            "louvain.greene_match",
            extra={"tenant": tenant, **stats},
        )

        # Delta computation: O(N log N) via bisect on the sorted gid
        # array — no auxiliary 30 MB set / dict needed.  Permutation
        # built once, original arrays released eagerly.
        order = sorted(range(len(gids_arr)), key=gids_arr.__getitem__)
        sorted_gids = array.array("q", (gids_arr[i] for i in order))
        sorted_olds = array.array("i", (olds_arr[i] for i in order))
        del gids_arr, olds_arr, order  # free ~5 MB eagerly

        delta: list[tuple[int, int]] = []
        n_prior = len(sorted_gids)
        for gid, raw in assignments.items():
            final = final_of_raw[raw]
            j = bisect.bisect_left(sorted_gids, gid)
            if j < n_prior and sorted_gids[j] == gid:
                if sorted_olds[j] != final:
                    delta.append((gid, final))
            else:
                # Vertex had no prior community_id (e.g. newly
                # written between recomputes) — unconditional write.
                delta.append((gid, final))

        log.info(
            "louvain.vertex_writeback_delta",
            extra={
                "tenant": tenant,
                "total": len(assignments),
                "delta": len(delta),
                "skipped": len(assignments) - len(delta),
            },
        )

    await writes.set_node_communities(pool, tenant=tenant, assignments=delta)

    # Edge writeback consumes the FINAL (post-Greene) IDs.  A
    # generator avoids materialising the full ~390 k-tuple list
    # before COPY hands it to PG; asyncpg's ``copy_records_to_table``
    # accepts any iterable.
    await writes.set_edge_end_communities(
        pool,
        tenant=tenant,
        assignments=(
            (gid, str(final_of_raw[raw])) for gid, raw in assignments.items()
        ),
    )
    return len(assignments), final_of_raw


async def _store_community_map(  # noqa: PLR0913 — the recompute's pieces
    pool: asyncpg.Pool,
    tenant: str,
    edges: graph_compute.EdgeArrays,
    assignments: Mapping[int, int],
    final_of_raw: Mapping[int, int],
    *,
    modularity: float,
) -> None:
    """Summarises the communities from the edges just clustered and stores
    the map. Best-effort: the communities are written already, and a map
    that failed is only missing until the next recompute."""
    try:
        data = await asyncio.to_thread(
            community_map.summarize, edges, assignments, final_of_raw,
        )
        await writes.store_community_map(pool, tenant, data, modularity=modularity)
    except (asyncpg.PostgresError, OSError, ValueError, KeyError) as exc:
        log.warning("community map not stored", extra={"tenant": tenant, "error": str(exc)})


# Shared with graph metrics (graphos.analysis); the old names stay.
GREENE_JACCARD_THETA = analysis.GREENE_JACCARD_THETA
_greene_match = analysis.greene_match
_build_node_filter_sql = analysis.build_node_filter_sql
_stream_semantic_edges = analysis.stream_edges


def _scope(
    rel_types: list[str] | None, node_filter: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The part of the graph a run read, when it did not read all of it."""
    scope = {"rel_types": rel_types, "node_filter": dict(node_filter) if node_filter else None}
    return {k: v for k, v in scope.items() if v} or None


async def recompute_communities(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    rel_types: list[str] | None = None,
    rel_weights: Mapping[str, float] | None = None,
    node_filter: Mapping[str, Any] | None = None,
    resolution: float = 1.0,
    min_community_size: int = MIN_COMMUNITY_SIZE_DEFAULT,
    seed: int = LOUVAIN_SEED,
    write: bool = True,
) -> LouvainResult:
    """Full Louvain pass over the tenant graph. Optionally write back.

    Args:
        node_filter: if set, only edges whose BOTH endpoints match every
                     ``{key: value | [values]}`` predicate are included.
                     This is the scoped-recompute pattern (e.g.
                     ``{"project_id": "<sid>"}`` runs Louvain over one
                     consumer-defined subgraph instead of the whole tenant).
        write: if False, runs the computation and returns the result
               without persisting `community_id`. Used for dry-run inspect.
    """
    t0 = _t.perf_counter()
    async with pool.acquire() as conn:
        started_at = await changes.head(conn)
    edges = await _stream_semantic_edges(
        pool, tenant=tenant, rel_types=rel_types, node_filter=node_filter,
    )
    t1 = _t.perf_counter()

    # In a child process: see graphos.louvain_compute for why.
    result = await louvain_compute.compute_in_process(
        edges, rel_weights=rel_weights,
        resolution=resolution, seed=seed, min_community_size=min_community_size,
    )
    assignments, sizes = result.assignments, result.sizes
    t2 = _t.perf_counter()
    largest = max(sizes.values(), default=0)

    written = 0
    if write and assignments:
        written, final_of_raw = await _writeback_assignments(
            pool, tenant=tenant, assignments=assignments,
        )
        await _store_community_map(
            pool, tenant, edges, assignments, final_of_raw, modularity=result.modularity,
        )
        await analysis.record_run(
            pool, tenant, "communities", started_at,
            scope=_scope(rel_types, node_filter),
        )
    t3 = _t.perf_counter()

    log.info(
        "louvain recompute complete",
        extra={
            "tenant": tenant,
            "n_nodes": result.n_nodes,
            "n_edges": len(edges),
            "n_communities": len(sizes),
            "n_assigned": len(assignments),
            "modularity": result.modularity,
        },
    )

    return LouvainResult(
        n_nodes=result.n_nodes,
        n_edges_scanned=len(edges),
        n_communities=len(sizes),
        n_assigned=written if write else len(assignments),
        n_singletons=result.n_singletons,
        largest_community_size=largest,
        modularity=result.modularity,
        edge_stream_ms=int((t1 - t0) * 1000),
        igraph_ms=int((t2 - t1) * 1000),
        writeback_ms=int((t3 - t2) * 1000),
    )

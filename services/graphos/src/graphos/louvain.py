"""In-process Louvain community detection over a tenant's graph.

Edges are streamed out of the AGE edge tables with plain SQL (integer
graphids only), fed to python-igraph's ``community_multilevel`` (Louvain
modularity optimisation, deterministic given a seed), and the assignments
are written back to vertex properties with COPY + one ``UPDATE ... FROM``.

Running Louvain in-process gives a deterministic memory ceiling and
keeps the storage backend free for reads during iteration.

Determinism: same edge set + same seed → same community_id integers.
Communities are relabelled by descending size so the IDs are stable
across runs, which makes diff-based monitoring (how many nodes changed community) cheap.

Single-flight: callers acquire a postgres advisory lock keyed on
``hashtext(tenant_name)`` before invoking. Two concurrent recomputes
of the same tenant → 409 at the API layer (see api/louvain.py).
"""

from __future__ import annotations

import array
import bisect
import logging
import random
import time as _t
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import asyncpg

from graphos import writes
from graphos.contracts.graph import _validate_prop_key
from graphos.db import allow_long_statements

# python-igraph is an optional extra (``pip install "graphos[louvain]"``)
# because it is GPL-licensed; the rest of graphos is Apache-2.0 and must
# import cleanly without it.  The recompute endpoint checks
# ``LOUVAIN_AVAILABLE`` and answers 501 when the extra is missing.
try:
    import igraph as ig
except ImportError:  # pragma: no cover - exercised by installs without the extra
    ig = None

LOUVAIN_AVAILABLE = ig is not None
LOUVAIN_INSTALL_HINT = (
    'Louvain community detection is not installed in this build. Install the '
    'optional extra (pip install "graphos[louvain]") or build the image with '
    '--build-arg GRAPHOS_EXTRAS=louvain. python-igraph is GPL-2.0-or-later.'
)

log = logging.getLogger(__name__)

LOUVAIN_SEED = 0xC0DE
MIN_COMMUNITY_SIZE_DEFAULT = 3

# Jaccard threshold for inheriting a prior community_id.  Greene 2010
# proposes 0.3; we choose the stricter 0.5 — a new cluster must share
# at least half of (its union with) a prior cluster to inherit that
# cluster's integer label, otherwise it mints a fresh ID.  Lower
# values risk "Ship of Theseus" drift where a cluster keeps the old
# ID while membership slowly diverges.
GREENE_JACCARD_THETA = 0.5


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


def _build_igraph(
    rows: list[tuple[int, int, str]],
    rel_weights: Mapping[str, float] | None,
) -> ig.Graph:
    """Turn (src_gid, dst_gid, rel_type) rows into an undirected igraph.

    Vertices are identified by AGE ``graphid`` (bigint).  Each row's
    weight comes from ``rel_weights`` (default 1.0).  Parallel edges
    between the same vertex pair are summed via
    ``simplify(combine_edges={"weight": "sum"})``.

    ``TupleList`` over ints (vs strings) is faster + smaller; igraph's
    own internal vertex naming uses the input value directly, so the
    caller's mapping to ``ext_id`` happens later via a single DB JOIN.
    """
    edges: list[tuple[int, int]] = []
    weights: list[float] = []
    for src_gid, dst_gid, rel_type in rows:
        if src_gid == dst_gid:
            continue
        edges.append((src_gid, dst_gid))
        w = (rel_weights or {}).get(rel_type, 1.0)
        weights.append(float(w))

    g = ig.Graph.TupleList(edges, directed=False)
    if weights:
        # TupleList preserves input ordering -- attach weights aligned
        # with the original ``edges`` list.
        g.es["weight"] = weights
        g.simplify(combine_edges={"weight": "sum"})
    else:
        g.simplify()
    return g


def _louvain(g: ig.Graph, *, resolution: float, seed: int) -> tuple[list[set[int]], float]:
    """Run Louvain. Returns (member-sets-per-community, modularity)."""
    if g.vcount() == 0:
        return [], 0.0
    weights = g.es["weight"] if "weight" in g.es.attributes() else None
    partition = g.community_multilevel(
        weights=weights, resolution=resolution, return_levels=False,
    )
    modularity = g.modularity(partition, weights=weights)
    return [set(c) for c in partition], float(modularity)


def _assign_and_relabel(
    partition: list[set[int]],
    vertex_names: list[int],
    min_size: int,
) -> tuple[dict[int, int], dict[int, int]]:
    """Drop tiny communities, relabel by descending size for stable IDs.

    ``vertex_names`` are AGE graphids (bigints) -- the keys of the
    returned ``assignments`` map.  The writeback step does ONE bulk
    UPDATE on the parent vertex inheritance table keyed by ``id``
    (primary-key) -- no agtype property lookup needed.

    Returns:
        (graphid → community_id, community_id → size)
    """
    sized = [(cid, members) for cid, members in enumerate(partition) if len(members) >= min_size]
    sized.sort(key=lambda p: (-len(p[1]), p[0]))  # ties broken by original cid

    assignments: dict[int, int] = {}
    sizes: dict[int, int] = {}
    for new_id, (_old_id, members) in enumerate(sized):
        sizes[new_id] = len(members)
        for vid in members:
            assignments[vertex_names[vid]] = new_id
    return assignments, sizes


def _greene_match(
    new_raw_of_gid: Mapping[int, int],
    new_sizes_by_raw: Mapping[int, int],
    pair_counts: Mapping[tuple[int, int], int],
    old_sizes: Mapping[int, int],
    *,
    theta: float = GREENE_JACCARD_THETA,
) -> tuple[dict[int, int], dict[str, int]]:
    """Map raw Louvain cluster IDs → final stable community IDs.

    Implements Greene/Doyle/Cunningham 2010, "Tracking the Evolution
    of Communities in Dynamic Social Networks" (ASONAM): each new
    cluster inherits the integer ID of the prior cluster it overlaps
    most with (by Jaccard ≥ ``theta``), falling back to a freshly
    minted ID otherwise.

    Pure function, no I/O.  Memory bounded by ``pair_counts`` (≤ N_new
    × avg-overlap-fanout, in practice a few thousand entries on a
    large tenant — sub-megabyte).  Caller is responsible for
    streaming the prior assignment via the DB read and emitting the
    delta against the *returned* mapping; this function never sees
    individual graphids.

    Determinism: new clusters processed largest-first with ties broken
    by raw ID ascending; old-cluster claims tracked in a set so
    splits and merges produce the same outputs across reruns on
    identical input.

    Args:
        new_raw_of_gid: ``{graphid → new_raw_cluster_id}`` — Louvain's
            raw partition keyed by AGE graphid.  Used only to derive
            ``new_sizes_by_raw``; not retained beyond that.
        new_sizes_by_raw: ``{new_raw_id → cluster_size}``.
        pair_counts: ``{(new_raw_id, old_id) → overlap_count}`` —
            built by the caller's single streaming pass over the
            prior assignment.
        old_sizes: ``{old_id → prior_cluster_size}`` — built in the
            same streaming pass.
        theta: Jaccard threshold for inheritance.

    Returns:
        ``(final_id_of_new_raw, stats)`` where ``stats`` carries
        ``inherited / minted / continuation / split / merge / birth /
        death`` counts for ops observability.

    Event taxonomy (per Greene 2010 §3.2):
        - continuation: 1 new ↔ 1 old, overlap ≥ θ — inherits.
        - split:        1 old → multiple new ≥ θ — largest child
                        inherits, siblings mint fresh.
        - merge:        multiple new each match same old ≥ θ —
                        first (largest) inherits, others mint fresh.
        - birth:        new with no match ≥ θ — mints fresh.
        - death:        old not claimed by anyone — no action here
                        (operators see it as a missing-from-output
                        ID gap; counted for logging only).
    """
    del new_raw_of_gid  # only used implicitly via new_sizes_by_raw

    # Group candidate matches per new cluster: {new_raw → [(old_id,
    # jaccard)...]} kept only above theta to bound memory.
    per_new: dict[int, list[tuple[int, float]]] = {}
    for (new_raw, old_id), inter in pair_counts.items():
        n = new_sizes_by_raw.get(new_raw, 0)
        o = old_sizes.get(old_id, 0)
        denom = n + o - inter
        if denom <= 0:
            continue
        j = inter / denom
        if j >= theta:
            per_new.setdefault(new_raw, []).append((old_id, j))

    # Process new clusters largest-first, ties broken by raw ID
    # ascending — deterministic across reruns on identical input.
    new_order = sorted(
        new_sizes_by_raw.keys(),
        key=lambda r: (-new_sizes_by_raw[r], r),
    )

    claimed: set[int] = set()
    final_id_of_new_raw: dict[int, int] = {}
    next_fresh_id = max(old_sizes.keys(), default=-1) + 1

    inherited = minted = 0
    splits_seen: set[int] = set()  # old_ids that lost children to ≥2 new
    merges_seen: set[int] = set()  # new_raws that matched ≥2 old above θ

    for new_raw in new_order:
        candidates = sorted(
            per_new.get(new_raw, ()),
            key=lambda p: (-p[1], p[0]),  # best Jaccard first, then id
        )
        if len(candidates) > 1:
            merges_seen.add(new_raw)
        chosen: int | None = None
        for old_id, _j in candidates:
            if old_id not in claimed:
                chosen = old_id
                break
        if chosen is not None:
            final_id_of_new_raw[new_raw] = chosen
            claimed.add(chosen)
            inherited += 1
        else:
            final_id_of_new_raw[new_raw] = next_fresh_id
            next_fresh_id += 1
            minted += 1

    # Splits: any old_id whose candidate-list mentions multiple new_raws.
    by_old: dict[int, int] = {}
    for cands in per_new.values():
        for old_id, _j in cands:
            by_old[old_id] = by_old.get(old_id, 0) + 1
    splits_seen = {oid for oid, c in by_old.items() if c > 1}

    deaths = len(set(old_sizes.keys()) - claimed)
    births = sum(1 for r in new_sizes_by_raw if not per_new.get(r))

    stats = {
        "inherited": inherited,
        "minted": minted,
        "continuations": inherited - len(merges_seen),
        "splits": len(splits_seen),
        "merges": len(merges_seen),
        "births": births,
        "deaths": deaths,
    }
    return final_id_of_new_raw, stats


def _build_node_filter_sql(
    node_filter: Mapping[str, Any] | None,
    src_alias: str = "src",
    dst_alias: str = "dst",
) -> tuple[list[str], list[Any]]:
    """Turn a ``{key: value | [values]}`` filter into SQL WHERE fragments.

    Each fragment compares ``<alias>.properties->'"<key>"'`` (agtype)
    against an agtype-quoted literal.  Both endpoints must match
    ("Louvain over the subgraph induced by this property").

    Returns (fragments, params) where ``$N`` placeholders in fragments
    line up positionally with params -- caller supplies them in order
    to ``conn.fetch``.
    """
    if not node_filter:
        return [], []
    frags: list[str] = []
    params: list[Any] = []
    for key, value in node_filter.items():
        _validate_prop_key(key)
        values = value if isinstance(value, list) else [value]
        # Build agtype-cast IN list: value goes through to_jsonb to
        # quote/escape, then ::agtype to match what AGE stored.
        for alias in (src_alias, dst_alias):
            placeholders = []
            for v in values:
                params.append(str(v))
                placeholders.append(f"to_jsonb(${len(params)}::text)::text::agtype")
            frags.append(
                f"{alias}.properties->'\"{key}\"' IN ({', '.join(placeholders)})"
            )
    return frags, params


async def _stream_semantic_edges(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    rel_types: list[str] | None,
    node_filter: Mapping[str, Any] | None = None,
) -> list[tuple[int, int, str]]:
    """Pull every (start_id, end_id, rel_type) row for the tenant.

    GRAPHID-ONLY transfer: the previous incarnation decoded
    ``properties->'"id"'`` twice per edge (once per endpoint) in SQL
    and shipped ~32-byte strings over the wire.  On a 6 M-edge graph
    that was 12 M agtype-operator evaluations + 400 MB of wire data
    + a multi-minute asyncpg row-decode loop.  Streaming the raw
    ``graphid`` (8-byte bigint each) skips the agtype decode and
    halves the wire payload -- the bottleneck shifts back to the
    JOIN scan itself (~40-60 s on 6 M edges) instead of agtype CPU.

    The ext_id strings are resolved ONCE at writeback time, only
    for the ~hundreds-of-thousands of vertices that actually got a
    community assignment.  Massively cheaper than per-edge decode.

    Filters compose:
      - ``rel_types``: post-JOIN filter on the child table name via
        ``c.relname IN (...)``.  When set, no vertex JOIN needed --
        we can read straight from the edge table.
      - ``node_filter``: per-endpoint property filter; needs the
        vertex inheritance JOIN to extract the property.  Rare path.

    Returns list of (src_graphid_bigint, dst_graphid_bigint, rel_type).
    """
    safe_rels: list[str] = []
    if rel_types:
        for rt in rel_types:
            _validate_prop_key(rt)
            safe_rels.append(rt)
    rel_clause = (
        f" AND c.relname IN ({', '.join(repr(r) for r in safe_rels)})"
        if safe_rels else ""
    )

    has_node_filter = bool(node_filter)
    filter_frags: list[str] = []
    filter_params: list[Any] = []
    if has_node_filter:
        filter_frags, filter_params = _build_node_filter_sql(node_filter)
    filter_clause = (" AND " + " AND ".join(filter_frags)) if filter_frags else ""

    if has_node_filter:
        # Full path with vertex JOINs (needed to filter on endpoint
        # properties).  Still drops the agtype decode of the
        # ``id`` property -- we ship graphids, not ext_ids.
        sql = (
            f"SELECT "
            f"  (e.start_id::text)::bigint AS src_gid,"
            f"  (e.end_id::text)::bigint   AS dst_gid,"
            f"  c.relname AS rel_type "
            f'FROM "{tenant}"."_ag_label_edge" e '
            f"JOIN pg_class c ON c.oid = e.tableoid "
            f'JOIN "{tenant}"."_ag_label_vertex" src ON src.id = e.start_id '
            f'JOIN "{tenant}"."_ag_label_vertex" dst ON dst.id = e.end_id '
            f"WHERE TRUE "
            f"{rel_clause}"
            f"{filter_clause};"
        )
    else:
        # Hot path: no node_filter -- skip the vertex JOINs entirely.
        # ``_ag_label_edge`` has start_id/end_id as graphid columns;
        # pg_class JOIN on tableoid is cheap (every child label has
        # one catalog row).
        sql = (
            f"SELECT "
            f"  (e.start_id::text)::bigint AS src_gid,"
            f"  (e.end_id::text)::bigint   AS dst_gid,"
            f"  c.relname AS rel_type "
            f'FROM "{tenant}"."_ag_label_edge" e '
            f"JOIN pg_class c ON c.oid = e.tableoid "
            f"WHERE TRUE "
            f"{rel_clause};"
        )

    async with pool.acquire() as conn:
        # 10-min budget for the one-shot all-edges sweep.  The pool's
        # default ``statement_timeout`` (60 s) would clip runs on large graphs.
        await allow_long_statements(conn, 600.0)
        rows = await conn.fetch(sql, *filter_params, timeout=600.0)

    return [
        (int(r["src_gid"]), int(r["dst_gid"]), str(r["rel_type"]))
        for r in rows
    ]


async def _writeback_assignments(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    assignments: Mapping[int, int],
    chunk_size: int = 500,  # noqa: ARG001 — kept for caller compat
) -> int:
    """Write `community_id` back onto each vertex.

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
        return 0

    # New-side aggregates: cluster sizes by RAW (size-sorted) ID,
    # built directly from the in-memory partition.  No DB round-trip.
    new_sizes_by_raw: dict[int, int] = {}
    for raw in assignments.values():
        new_sizes_by_raw[raw] = new_sizes_by_raw.get(raw, 0) + 1

    # Streaming accumulators.  ``array.array`` typecodes:
    #   'q' = signed int64 (graphid)   — 8 B
    #   'i' = signed int32 (community) — 4 B
    # Per-vertex client memory ~12 B vs ~140 B for a dict[int,str].
    gids_arr: array.array = array.array("q")
    olds_arr: array.array = array.array("i")
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
            raw = assignments.get(gid)
            if raw is not None:
                key = (raw, old)
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
    return len(assignments)


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
    rows = await _stream_semantic_edges(
        pool, tenant=tenant, rel_types=rel_types, node_filter=node_filter,
    )
    t1 = _t.perf_counter()

    g = _build_igraph(rows, rel_weights)
    # python-igraph community_multilevel doesn't yet expose `seed=` as
    # a kwarg in 0.11 (it picks it up from the random state). We seed
    # via the module-level RNG before the call.
    ig.set_random_number_generator(random.Random(seed))
    partition, modularity = _louvain(g, resolution=resolution, seed=seed)
    t2 = _t.perf_counter()

    vertex_names = [v["name"] for v in g.vs] if g.vcount() else []
    assignments, sizes = _assign_and_relabel(partition, vertex_names, min_community_size)
    n_singletons = sum(1 for members in partition if 0 < len(members) < min_community_size)
    largest = max(sizes.values(), default=0)

    written = 0
    if write and assignments:
        written = await _writeback_assignments(pool, tenant=tenant, assignments=assignments)
    t3 = _t.perf_counter()

    log.info(
        "louvain recompute complete",
        extra={
            "tenant": tenant,
            "n_nodes": g.vcount(),
            "n_edges": len(rows),
            "n_communities": len(sizes),
            "n_assigned": len(assignments),
            "modularity": modularity,
        },
    )

    return LouvainResult(
        n_nodes=g.vcount(),
        n_edges_scanned=len(rows),
        n_communities=len(sizes),
        n_assigned=written if write else len(assignments),
        n_singletons=n_singletons,
        largest_community_size=largest,
        modularity=modularity,
        edge_stream_ms=int((t1 - t0) * 1000),
        igraph_ms=int((t2 - t1) * 1000),
        writeback_ms=int((t3 - t2) * 1000),
    )

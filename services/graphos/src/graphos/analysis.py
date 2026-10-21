"""What Louvain and graph metrics share: one heavy job per tenant, the edge
stream, and stable ids across runs.

* :func:`tenant_lock`: one whole-graph job (a Louvain recompute or a metrics
  run) per tenant at a time. Both rewrite every node; run together they would
  fight over the same rows and hold the full edge list twice.
* :func:`stream_edges`: every (source graphid, target graphid, type) of the
  tenant, into compact :class:`~graphos.graph_compute.EdgeArrays`.
* :func:`greene_match`: carries group ids (communities, components) over from
  the previous run, so an unchanged graph keeps its ids.
* :func:`vacuum_after_writeback`: a write-back rewrites a row per node (or
  edge) changed, leaving as many dead rows; a ``VACUUM (ANALYZE)`` of the
  tables written then makes that space reusable at once, so the next run
  fills it instead of growing the tables (a first metrics and Louvain run on
  a fresh tenant otherwise left it about 2.7 times its loaded size until
  autovacuum came round).  It does not give the space back to the system
  (``VACUUM FULL`` would, under an exclusive lock).
* :func:`record_run` / :func:`freshness`: when each analysis last wrote its
  results and how much was written since (``graphos_meta.analysis_runs``).
  The position is taken before the run reads, so a write that raced the run
  counts as "since": a run may be reported stale, never fresh by mistake.
  A metrics run that changes nothing records no change, so the change feed
  alone cannot say when one ran; this table can.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from graphos import changes
from graphos.contracts.graph import _validate_prop_key
from graphos.db import allow_long_statements
from graphos.graph_compute import EdgeArrays

log = logging.getLogger(__name__)

# Jaccard threshold for inheriting a prior id.  Greene 2010 proposes 0.3;
# we choose the stricter 0.5 — a new group must share at least half of (its
# union with) a prior group to inherit that group's integer id, otherwise it
# mints a fresh one.  Lower values risk "Ship of Theseus" drift where a group
# keeps the old id while membership slowly diverges.
GREENE_JACCARD_THETA = 0.5

_EDGE_BATCH = 20_000


class TenantBusyError(Exception):
    """Another whole-graph job holds the tenant."""


@contextlib.asynccontextmanager
async def tenant_lock(pool: asyncpg.Pool, tenant: str) -> AsyncIterator[None]:
    """Hold the tenant's whole-graph lock, or raise :class:`TenantBusyError`.

    A Postgres advisory lock on a 64-bit hash of the tenant name (a 32-bit
    hash lets two tenants' names collide and block each other). Seed 1 keeps
    it apart from tenant setup's lock on the same name (bootstrap.py, seed 0),
    so a job never holds up registration of its own tenant. Session-level,
    on a connection held for the job, so every replica sees it.
    """
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 1))", tenant):
            raise TenantBusyError(tenant)
        try:
            yield
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 1))", tenant)


def greene_match(
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


def build_node_filter_sql(
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


async def stream_edges(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    rel_types: list[str] | None,
    node_filter: Mapping[str, Any] | None = None,
) -> EdgeArrays:
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

    Rows are read through a server-side cursor in batches straight into
    :class:`~graphos.EdgeArrays`, so neither a full result set nor a tuple per edge
    is ever held in memory, and the event loop runs between batches.
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
        filter_frags, filter_params = build_node_filter_sql(node_filter)
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

    edges = EdgeArrays()
    code_of: dict[str, int] = {}
    async with pool.acquire() as conn, conn.transaction():
        # 10-min budget for the one-shot all-edges sweep.  The pool's
        # default ``statement_timeout`` (60 s) would clip runs on large graphs.
        await allow_long_statements(conn, 600.0)
        cursor = await conn.cursor(sql, *filter_params)
        while batch := await cursor.fetch(_EDGE_BATCH, timeout=600.0):
            for src_gid, dst_gid, rel_type in batch:
                code = code_of.get(rel_type)
                if code is None:
                    code = code_of[rel_type] = len(edges.rel_names)
                    edges.rel_names.append(rel_type)
                edges.src.append(src_gid)
                edges.dst.append(dst_gid)
                edges.rel.append(code)
    return edges


# ── When each analysis last ran ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Freshness:
    """An analysis's last written run, and what was written to the graph since."""

    computed_at: datetime
    scope: dict[str, Any] | None
    rows_written_since: int | None    # None: the records since were pruned
    at_least: bool                    # the count stopped at its cap


# A write-back of fewer rows than this leaves too little to clean up for a
# VACUUM to be worth its time; autovacuum handles it.
VACUUM_AFTER_ROWS = 10_000
_VACUUM_TIMEOUT_S = 1800.0


async def vacuum_after_writeback(
    pool: asyncpg.Pool, tenant: str, kind: str, rows_written: int,
) -> None:
    """``VACUUM (ANALYZE)`` the tenant's vertex (``kind="vertex"``) or edge
    (``"edge"``) label tables after a write-back of ``rows_written`` rows.

    Best-effort: the results are written already, so a failure is logged,
    never raised.  VACUUM cannot run in a transaction: one statement per
    table, each on its own.
    """
    if rows_written < VACUUM_AFTER_ROWS:
        return
    started = time.perf_counter()
    try:
        async with pool.acquire() as conn:
            labels = [str(r["relname"]) for r in await conn.fetch(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_inherits i ON i.inhrelid = c.oid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE n.nspname = $1 AND p.relname = $2 AND c.relkind = 'r' "
                "ORDER BY c.relname",
                tenant, f"_ag_label_{kind}",
            )]
            await allow_long_statements(conn, _VACUUM_TIMEOUT_S)
            for label in labels:
                await conn.execute(
                    f'VACUUM (ANALYZE) "{tenant}"."{label}"', timeout=_VACUUM_TIMEOUT_S,
                )
    except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
        log.warning(
            "vacuum after write-back failed",
            extra={"tenant": tenant, "kind": kind, "error": f"{type(exc).__name__}: {exc}"},
        )
        return
    log.info(
        "vacuum after write-back",
        extra={"tenant": tenant, "kind": kind, "tables": len(labels),
               "rows_written": rows_written,
               "elapsed_ms": int((time.perf_counter() - started) * 1000)},
    )


async def record_run(  # noqa: PLR0913 — a run's parts
    pool: asyncpg.Pool, tenant: str, kind: str, started_at: changes.Cursor,
    scope: Mapping[str, Any] | None = None, params: Mapping[str, Any] | None = None,
) -> None:
    """Remember that ``kind`` (``metrics`` or ``communities``) wrote its results,
    computed from data as of the feed position ``started_at``, over ``scope``
    and with ``params`` (how it computed)."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO graphos_meta.analysis_runs
                (tenant, kind, computed_at, txid, seq, scope, params)
            VALUES ($1, $2, now(), $3::text::xid8, $4, $5::jsonb, $6::jsonb)
            ON CONFLICT (tenant, kind) DO UPDATE
              SET computed_at = now(), txid = EXCLUDED.txid, seq = EXCLUDED.seq,
                  scope = EXCLUDED.scope, params = EXCLUDED.params
            """,
            tenant, kind, str(started_at.txid), started_at.seq,
            json.dumps(dict(scope)) if scope else None,
            json.dumps(dict(params)) if params else None,
        )


async def last_run(
    pool: asyncpg.Pool, tenant: str, kind: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None] | None:
    """The ``(scope, params)`` of ``kind``'s last run on ``tenant``; None if none."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT scope::text AS scope, params::text AS params "
            "FROM graphos_meta.analysis_runs WHERE tenant = $1 AND kind = $2;",
            tenant, kind,
        )
    if row is None:
        return None
    return (json.loads(row["scope"]) if row["scope"] else None,
            json.loads(row["params"]) if row["params"] else None)


async def freshness(conn: asyncpg.Connection, tenant: str) -> dict[str, Freshness]:
    """Each analysis of ``tenant`` that has run, by kind."""
    out: dict[str, Freshness] = {}
    for r in await conn.fetch(
        "SELECT kind, computed_at, txid::text AS txid_text, seq, scope::text AS scope_text "
        "FROM graphos_meta.analysis_runs WHERE tenant = $1 ORDER BY kind;",
        tenant,
    ):
        since = await changes.written_since(
            conn, tenant, changes.Cursor(int(r["txid_text"]), int(r["seq"])),
        )
        out[r["kind"]] = Freshness(
            computed_at=r["computed_at"],
            scope=json.loads(r["scope_text"]) if r["scope_text"] else None,
            rows_written_since=None if since is None else since[0],
            at_least=bool(since and since[1]),
        )
    return out

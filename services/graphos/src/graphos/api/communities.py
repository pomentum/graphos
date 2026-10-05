"""Read endpoints over Louvain output + tenant stats.

Three endpoints, one router:

  GET /v1/{tenant}/graph/communities?min_size=&include_members=&top=
  GET /v1/{tenant}/graph/community-summary
  GET /v1/{tenant}/graph/stats

These are the "what did Louvain produce" + "how big is my graph"
endpoints. All pure reads — no writes, no advisory locks.
"""

from __future__ import annotations

import time
from typing import Annotated

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, status

from graphos.api.graph import _check_tenant
from graphos.api.tenants import Principal
from graphos.bootstrap import registered_tenants
from graphos.contracts.communities import (
    CommunitiesResponse,
    CommunityIdsRequest,
    CommunityIdsResponse,
    CommunityRow,
    CommunitySummary,
    HistogramResponse,
    HistogramRow,
    LabelCount,
    TenantStatsResponse,
    TopByEdgeResponse,
    TopByEdgeRow,
)

# Upper bound on ``sample_labels``; larger requests fall back to the default.
MAX_SAMPLE_LABELS = 100

router = APIRouter(tags=["communities"], prefix="/{tenant}/graph")

DEFAULT_MIN_COMMUNITY_READ_SIZE = 1
DEFAULT_TOP_N = 10
MAX_TOP_N = 100
MAX_INCLUDED_MEMBERS_PER_COMMUNITY = 500
# Pagination cap for the /communities listing.  Large tenants
# can produce 150-300 communities; a graph-explorer client typically pages
# at 50/page so we default to that and cap at 500 (≈ "show me everything"
# for any realistic graph).  ``limit=0`` is reserved to mean "no cap"
# for callers that genuinely need the full enumeration.
DEFAULT_COMMUNITY_LIMIT = 50
MAX_COMMUNITY_LIMIT = 500
# Number of distinct label names to include in each community's
# ``sample_labels``.  Matches the prior _sample_labels(limit=5) call.
SAMPLE_LABELS_PER_COMMUNITY = 5


# ── helpers ────────────────────────────────────────────────────────────────


async def _members_of_communities(
    request: Request, tenant: str, community_ids: list[int], limit: int,
) -> dict[int, list[str]]:
    """Up to ``limit`` member ids for each community, in one query.

    For each community, the per-label index registration creates on
    ``community_id`` (``<Label>_community_id``) is read until ``limit``
    members are found, so the cost is bounded by ``limit`` per community
    rather than by community size (the largest communities hold most of a
    large graph).
    """
    if not community_ids:
        return {}
    rows = await request.app.state.pool.fetch(
        f"""
        SELECT c.cid, m.ext_id
        FROM unnest($1::text[]) AS c(cid)
        CROSS JOIN LATERAL (
            SELECT (v.properties -> '"id"')::text AS ext_id
            FROM "{tenant}"."_ag_label_vertex" v
            WHERE ((v.properties::text)::jsonb ->> 'community_id') = c.cid
              AND (v.properties -> '"id"') IS NOT NULL
            LIMIT $2
        ) m;
        """,
        [str(c) for c in community_ids], limit,
    )
    members: dict[int, list[str]] = {c: [] for c in community_ids}
    for r in rows:
        members[int(r["cid"])].append(str(r["ext_id"]))
    return members


async def _communities_with_labels(
    request: Request,
    tenant: str,
    *,
    min_size: int,
    limit: int | None,
    sample_labels: int = SAMPLE_LABELS_PER_COMMUNITY,
) -> tuple[list[tuple[int, int, list[str]]], int, int]:
    """Return ``[(community_id, size, sample_labels), …]`` + assigned/unassigned.

    One scan of the parent ``_ag_label_vertex`` table (inheritance reaches
    every label table), grouped by ``community_id``: the vertices without
    one form the NULL group, which gives the unassigned count, and the sum
    of the other groups gives the assigned count, so no second or third
    scan is needed.  ``array_agg(DISTINCT relname)`` rolls the labels up per
    community, alphabetically, first ``sample_labels`` of them.  Filtering
    by ``min_size``, ordering (size descending, then id) and ``limit``
    apply to the grouped rows, one per community.
    """
    pool = request.app.state.pool
    # ``sample_labels`` is a small bounded int, interpolated into the array
    # slice (a ``$N::int`` placeholder inside a subscript does not always plan).
    sample_n = int(sample_labels)
    if sample_n < 1 or sample_n > MAX_SAMPLE_LABELS:
        sample_n = SAMPLE_LABELS_PER_COMMUNITY
    cid = """((v.properties->'"community_id"')::text)::int"""
    sql = f"""
        SELECT
          {cid} AS cid,
          count(*) AS sz,
          (array_agg(DISTINCT c.relname ORDER BY c.relname)
             FILTER (WHERE {cid} IS NOT NULL))[1:{sample_n}] AS sample_labels
        FROM "{tenant}"."_ag_label_vertex" v
        JOIN pg_class c ON c.oid = v.tableoid
        GROUP BY {cid}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)

    n_unassigned = sum(int(r["sz"]) for r in rows if r["cid"] is None)
    groups = [r for r in rows if r["cid"] is not None]
    n_assigned = sum(int(r["sz"]) for r in groups)
    kept = sorted(
        (r for r in groups if int(r["sz"]) >= min_size),
        key=lambda r: (-int(r["sz"]), int(r["cid"])),
    )
    if limit is not None and limit > 0:
        kept = kept[:limit]
    out = [
        (
            int(r["cid"]),
            int(r["sz"]),
            [str(lbl) for lbl in (r["sample_labels"] or []) if lbl],
        )
        for r in kept
    ]
    return out, n_assigned, n_unassigned


# ── /communities ───────────────────────────────────────────────────────────


@router.get(
    "/communities",
    response_model=CommunitiesResponse,
    summary="List communities + sizes from the last Louvain assignment",
    description=(
        "Reads `community_id` written by the most recent recompute. "
        "Filter out tiny communities via `min_size`.  Use ``limit`` to "
        "paginate (default 50, max "
        f"{MAX_COMMUNITY_LIMIT}; pass 0 for unbounded — caller's "
        "responsibility on large tenants).  Set "
        "`include_members=true` to return up to "
        f"{MAX_INCLUDED_MEMBERS_PER_COMMUNITY} member ids per community."
    ),
)
async def list_communities(
    tenant: str,
    request: Request,
    principal: Principal,
    min_size: Annotated[int, Query(ge=1)] = DEFAULT_MIN_COMMUNITY_READ_SIZE,
    include_members: Annotated[bool, Query()] = False,
    limit: Annotated[
        int, Query(ge=0, le=MAX_COMMUNITY_LIMIT,
                   description="Max communities to return; 0 = no cap."),
    ] = DEFAULT_COMMUNITY_LIMIT,
) -> CommunitiesResponse:
    _check_tenant(tenant, principal)
    started = time.perf_counter()
    try:
        # Single SQL round-trip for size + sample_labels (was a Cypher
        # per community — the dominant cost on large graphs).
        triples, n_assigned, n_unassigned = await _communities_with_labels(
            request, tenant,
            min_size=min_size,
            limit=(limit if limit > 0 else None),
        )
        members = (
            await _members_of_communities(
                request, tenant, [cid for cid, _, _ in triples],
                limit=MAX_INCLUDED_MEMBERS_PER_COMMUNITY,
            )
            if include_members else {}
        )
        out = [
            CommunityRow(
                community_id=cid,
                size=size,
                sample_labels=labels,
                member_ids=members.get(cid) if include_members else None,
            )
            for cid, size, labels in triples
        ]
    except asyncpg.exceptions.UndefinedTableError as exc:
        # Tenant graph doesn't exist.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"tenant not registered: {exc}",
        ) from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return CommunitiesResponse(
        tenant=tenant,
        min_size=min_size,
        n_communities=len(out),
        n_assigned_nodes=n_assigned,
        n_unassigned_nodes=n_unassigned,
        elapsed_ms=elapsed_ms,
        communities=out,
    )


# ── /community-summary ─────────────────────────────────────────────────────


@router.get(
    "/community-summary",
    response_model=CommunitySummary,
    summary="Aggregate stats over the last Louvain output",
)
async def community_summary(
    tenant: str,
    request: Request,
    principal: Principal,
    top: Annotated[int, Query(ge=1, le=MAX_TOP_N)] = DEFAULT_TOP_N,
) -> CommunitySummary:
    _check_tenant(tenant, principal)
    started = time.perf_counter()
    try:
        # Pull the full histogram (no limit) once so we can report
        # n_communities + min/max sizes accurately, then slice the
        # first ``top`` for the response.  One SQL round trip instead of
        # 1 + N per-community queries.
        triples, n_assigned, n_unassigned = await _communities_with_labels(
            request, tenant, min_size=1, limit=None,
        )
        top_rows: list[CommunityRow] = [
            CommunityRow(
                community_id=cid,
                size=size,
                sample_labels=labels,
            )
            for cid, size, labels in triples[:top]
        ]
    except asyncpg.exceptions.UndefinedTableError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"tenant not registered: {exc}",
        ) from exc

    sizes = [s for _, s, _ in triples]
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return CommunitySummary(
        tenant=tenant,
        n_communities=len(triples),
        n_assigned_nodes=n_assigned,
        n_unassigned_nodes=n_unassigned,
        largest_community_size=max(sizes, default=0),
        smallest_community_size=min(sizes, default=0),
        top=top_rows,
        elapsed_ms=elapsed_ms,
    )


# ── /stats (per-tenant graph size) ─────────────────────────────────────────


@router.get(
    "/stats",
    response_model=TenantStatsResponse,
    summary="Per-tenant graph stats — vertex/edge counts by label, total bytes",
    description=(
        "Exact `count(*)` per label over AGE's per-label storage tables "
        "(cheaper than Cypher `count()`). `total_bytes` is the rolled-up "
        "`pg_total_relation_size` across all of the tenant's "
        "`_ag_label_*` tables."
    ),
)
async def tenant_stats(
    tenant: str,
    request: Request,
    principal: Principal,
) -> TenantStatsResponse:
    _check_tenant(tenant, principal)
    started = time.perf_counter()
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        # AGE stores each label in its own table inheriting from
        # _ag_label_vertex / _ag_label_edge. The graph's per-label
        # entries live in `ag_catalog.ag_label`; `kind` is "char"
        # (single byte 'v' or 'e') — we cast to text to dodge asyncpg's
        # binary encoding of that type.
        rows = await conn.fetch(
            """
            SELECT
              l.name                                AS label_name,
              l.kind::text                          AS kind,
              c.oid::regclass::text                 AS qualified_name,
              pg_total_relation_size(c.oid)         AS total_bytes
            FROM ag_catalog.ag_label l
            JOIN ag_catalog.ag_graph g  ON g.graphid = l.graph
            JOIN pg_class c             ON c.oid     = l.relation
            WHERE g.name = $1
              AND l.name NOT IN ('_ag_label_vertex', '_ag_label_edge')
            ORDER BY pg_total_relation_size(c.oid) DESC;
            """,
            tenant,
        )

        # Per-label exact `count(*)` — accurate without depending on
        # autovacuum having analyzed the tables. `oid::regclass::text`
        # produces a properly-quoted schema."table" string that's safe
        # to interpolate. Fast on AGE's per-label storage.
        labels: list[LabelCount] = []
        n_vertices = 0
        n_edges = 0
        total_bytes = 0
        for r in rows:
            size = int(r["total_bytes"] or 0)
            kind = "v" if str(r["kind"]).strip() == "v" else "e"
            n = await conn.fetchval(
                f"SELECT count(*) FROM {r['qualified_name']}",
            )
            count = int(n or 0)
            labels.append(
                LabelCount(
                    label=r["label_name"],
                    kind=kind,
                    count=count,
                    total_bytes=size,
                ),
            )
            if kind == "v":
                n_vertices += count
            else:
                n_edges += count
            total_bytes += size

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return TenantStatsResponse(
        tenant=tenant,
        n_vertices=n_vertices,
        n_edges=n_edges,
        total_bytes=total_bytes,
        labels=labels,
        elapsed_ms=elapsed_ms,
    )


# ── /community-ids (batched lookup) ────────────────────────────────────────


# Hard cap on ids per request — keeps the agtype[] parameter
# bounded and prevents an accidental "look up the whole tenant" call
# from blowing the asyncpg statement cache.  Callers typically ask
# for 20-50 ids per query; 500 is ~10× headroom.
MAX_COMMUNITY_IDS_LOOKUP = 500


@router.post(
    "/community-ids",
    response_model=CommunityIdsResponse,
    summary="Batched external id → community_id lookup",
    description=(
        "Replaces an N+1 ``/search``-per-node loop.  One SQL round-trip "
        "via the per-label ``<Label>_id_unique`` btrees; ~3-50ms for a "
        "typical 20-50-id lookup.  At most 500 ids are looked up; "
        "`truncated` says when more were sent."
    ),
)
async def community_ids(
    tenant: str,
    request: Request,
    principal: Principal,
    body: CommunityIdsRequest,
) -> CommunityIdsResponse:
    _check_tenant(tenant, principal)
    started = time.perf_counter()
    # Truncate at the cap and say so in ``truncated``: community data is
    # best-effort by design, so partial results beat a 400 that drops the
    # caller's whole query.
    truncated = len(body.ids) > MAX_COMMUNITY_IDS_LOOKUP
    raw_ids = body.ids[:MAX_COMMUNITY_IDS_LOOKUP]
    requested = len(raw_ids)
    # Direct SQL against the parent ``_ag_label_vertex`` table —
    # inheritance makes one query cover every label.  The per-label
    # ``<Label>_id_unique`` btrees give O(log N) per lookup;
    # the planner uses an Index Scan over an Append node across the
    # label children (verified via EXPLAIN ANALYZE — ~3ms for 2 ids,
    # scales linearly with input size).
    #
    # Performance-critical SQL: match the indexed agtype expression
    # so the planner uses the ``<Label>_id_unique`` btrees for
    # an index scan over the Append node — NOT a sequential scan on
    # every label child.
    #
    # The unique indexes are defined as
    # ``btree (((properties -> '"id"'::agtype)))`` — i.e. on the
    # raw agtype expression, with no cast.  A WHERE clause of the
    # shape ``(properties->'"id"')::text = ANY($1::text[])`` does
    # NOT match because PostgreSQL treats the cast as a different
    # expression than the indexed one (verified via EXPLAIN ANALYZE
    # — parallel seq scan across every label, ~220ms for 2 ids).
    #
    # The agtype textual form of a string scalar is
    # ``'"value"'::agtype`` (with the wrapping quotes), so we
    # convert the caller's plain text ids to agtype scalars
    # server-side via ``unnest($1) -> '"..."'::agtype``.  Doing the
    # conversion in SQL (not Python) keeps the asyncpg type binding
    # at the well-supported ``text[]`` and lets the planner inline
    # the per-row cast.
    #
    # ``trim`` strips the wrapping quotes back off in the SELECT
    # for the response; ``::int`` decodes community_id (agtype
    # integer -> int4 via two-step text cast — same pattern Louvain
    # writeback uses).
    pool = request.app.state.pool
    out: dict[str, int] = {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
              trim(both '"' from (v.properties->'"id"')::text)
                                                   AS ext_id,  
              ((v.properties->'"community_id"')::text)::int
                                                   AS community_id
            FROM "{tenant}"."_ag_label_vertex" v
            WHERE (v.properties->'"id"') = ANY(
              ARRAY(
                SELECT ('"' || t || '"')::agtype
                FROM unnest($1::text[]) AS t
              )
            )
              AND (v.properties->'"community_id"') IS NOT NULL
            """,
            raw_ids,
        )
    for r in rows:
        # Defensive — if an id contains a literal quote it would
        # round-trip with embedded escapes; ``trim`` strips only the
        # outermost pair, the same shape json.dumps emitted on input.
        out[str(r["ext_id"])] = int(r["community_id"])
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return CommunityIdsResponse(
        tenant=tenant,
        n_requested=requested,
        n_resolved=len(out),
        community_by_id=out,
        truncated=truncated,
        elapsed_ms=elapsed_ms,
    )


# ── /labels/{label}/histogram + /labels/{label}/top ────────────────────────
#
# Schema-agnostic analytics.  Callers parameterise the label, the
# property to bucket on (histogram), or the edge to count (top-by-edge).
# Each replaces N sequential per-label scans a consumer would otherwise run.

# Sanity caps — protect graphos from a runaway scan if a caller asks
# for "histogram every value of a free-text property."
MAX_HISTOGRAM_LIMIT = 1000
DEFAULT_HISTOGRAM_LIMIT = 100
MAX_TOP_K = 200
DEFAULT_TOP_K = 20


def _check_label(tenant: str, label: str, *, kind: str) -> None:
    """Validate that *label* is registered for *tenant*.

    Required because the label name flows into a quoted SQL identifier
    in the queries below.  ``registered_tenants()`` is the in-process
    spec map populated on tenant register + restore-on-startup; the
    lookup is sub-microsecond.  Rejecting unknown labels here keeps
    every SQL string a closed set of identifiers the server has
    already vetted at register time.
    """
    spec = registered_tenants().get(tenant)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"tenant {tenant!r} not registered",
        )
    allowed = set(spec.vertex_labels) if kind == "vertex" else set(spec.edge_labels)
    if label not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"{kind} label {label!r} is not registered for tenant "
                f"{tenant!r}.  Register it via POST /v1/tenants."
            ),
        )


@router.get(
    "/labels/{label}/histogram",
    response_model=HistogramResponse,
    summary="Property-value histogram for one label",
    description=(
        "Count nodes of ``label`` grouped by a property value.  Replaces "
        "a pattern of N sequential Cypher queries (one per value) "
        "with one SQL ``GROUP BY`` against the per-label storage table — "
        "~1-50ms even on large tenants.  Schema-agnostic: pick any "
        "registered label and any property name; missing-value rows are "
        "reported under value ``\"\"`` (empty string)."
    ),
)
async def label_histogram(
    tenant: str,
    label: str,
    request: Request,
    principal: Principal,
    property: Annotated[
        str, Query(..., min_length=1, max_length=128,
                   description="Property name to bucket on."),
    ],
    limit: Annotated[
        int, Query(ge=1, le=MAX_HISTOGRAM_LIMIT),
    ] = DEFAULT_HISTOGRAM_LIMIT,
) -> HistogramResponse:
    _check_tenant(tenant, principal)
    _check_label(tenant, label, kind="vertex")
    started = time.perf_counter()
    pool = request.app.state.pool
    # ``properties->'"<key>"'``: agtype value of the property.  ``::text``
    # strips the wrapping quotes for string scalars; numeric scalars
    # already render as bare numbers.  ``trim`` removes the outer
    # double-quotes added by the JSON-text cast so callers see
    # ``"foo"`` instead of ``"\"foo\""``.  COALESCE on NULL preserves
    # the missing-value bucket as the empty string.
    # Single query: bins + totals via ``SUM(count) OVER ()`` window.
    # Avoids a second ``COUNT(DISTINCT
    # properties->'"X"')`` on the full label table — verified slow
    # (~2-5s on 281K-row tables because COUNT DISTINCT on a jsonb
    # expression can't use the per-property GIN).
    #
    # The window emits the same ``n_total`` on every row; we read
    # it off the first row.  ``n_distinct`` is no longer cheap to
    # compute on a value-extracted GROUP BY; we report it only as
    # ``len(bins)`` when the bins were not truncated (caller can
    # detect truncation via ``len(bins) == limit``).  When ``limit``
    # is hit we leave ``n_distinct`` at -1 as the "not-known" sentinel
    # to keep the response shape stable.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
              COALESCE(
                trim(both '"' from (v.properties->('"' || $1::text || '"')::agtype)::text),
                ''
              ) AS value,
              count(*) AS c,
              SUM(count(*)) OVER ()::bigint AS n_total
            FROM "{tenant}"."{label}" v
            GROUP BY 1
            ORDER BY c DESC, value ASC
            LIMIT $2;
            """,
            property,
            limit,
        )

    n_total = int(rows[0]["n_total"]) if rows else 0
    truncated = len(rows) >= limit
    # n_distinct is the exact count when we didn't truncate, else
    # the not-known sentinel (-1).  Callers that need the true
    # distinct count when truncated should re-query with a bigger
    # ``limit`` — the bins themselves carry the same information.
    n_distinct = len(rows) if not truncated else -1

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return HistogramResponse(
        tenant=tenant,
        label=label,
        property=property,
        n_distinct=n_distinct,
        n_total=n_total,
        bins=[
            HistogramRow(value=str(r["value"]), count=int(r["c"]))
            for r in rows
        ],
        elapsed_ms=elapsed_ms,
    )


@router.get(
    "/labels/{label}/top",
    response_model=TopByEdgeResponse,
    summary="Top-K nodes of a label by edge-degree on a relation",
    description=(
        "For each node of ``label``, count edges of type ``by_edge`` in "
        "the chosen direction (``out``/``in``/``both``); return the top "
        "``k`` by count.  Replaces per-consumer aggregate scans (e.g. "
        "container-membership counters) with one schema-agnostic SQL.  "
        "Uses the ``(start_id, end_id)`` btree on the edge label table "
        "for an index-scan ``GROUP BY``."
    ),
)
async def label_top_by_edge(
    tenant: str,
    label: str,
    request: Request,
    principal: Principal,
    by_edge: Annotated[
        str, Query(..., min_length=1, max_length=128,
                   description="Edge label / relationship type to count."),
    ],
    direction: Annotated[
        str, Query(pattern="^(out|in|both)$"),
    ] = "out",
    k: Annotated[int, Query(ge=1, le=MAX_TOP_K)] = DEFAULT_TOP_K,
) -> TopByEdgeResponse:
    _check_tenant(tenant, principal)
    _check_label(tenant, label, kind="vertex")
    _check_label(tenant, by_edge, kind="edge")
    started = time.perf_counter()
    pool = request.app.state.pool

    # Three SQL shapes; ``out`` counts edges where the parent is the
    # source, ``in`` where parent is the destination, ``both`` is the
    # UNION ALL summed.  Each shape JOINs the edge table to the parent
    # label table on the AGE-graphid PK so the planner uses the
    # ``(start_id, end_id)`` btree.
    if direction == "out":
        join_col = "start_id"
    elif direction == "in":
        join_col = "end_id"
    else:
        join_col = None  # marker for the UNION branch

    async with pool.acquire() as conn:
        if join_col is not None:
            rows = await conn.fetch(
                f"""
                SELECT
                  trim(both '"' from (v.properties->'"id"')::text) AS node_id,
                  count(e.*) AS edge_count
                FROM "{tenant}"."{by_edge}" e
                JOIN "{tenant}"."{label}" v ON v.id = e.{join_col}
                GROUP BY v.id, v.properties
                ORDER BY edge_count DESC, node_id ASC
                LIMIT $1;
                """,
                k,
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT node_id, sum(c) AS edge_count FROM (
                  SELECT
                    trim(both '"' from (v.properties->'"id"')::text) AS node_id,
                    count(*) AS c
                  FROM "{tenant}"."{by_edge}" e
                  JOIN "{tenant}"."{label}" v ON v.id = e.start_id
                  GROUP BY v.id, v.properties
                  UNION ALL
                  SELECT
                    trim(both '"' from (v.properties->'"id"')::text) AS node_id,
                    count(*) AS c
                  FROM "{tenant}"."{by_edge}" e
                  JOIN "{tenant}"."{label}" v ON v.id = e.end_id
                  GROUP BY v.id, v.properties
                ) u
                GROUP BY node_id
                ORDER BY edge_count DESC, node_id ASC
                LIMIT $1;
                """,
                k,
            )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return TopByEdgeResponse(
        tenant=tenant,
        label=label,
        by_edge=by_edge,
        direction=direction,
        rows=[
            TopByEdgeRow(
                node_id=str(r["node_id"]),
                edge_count=int(r["edge_count"]),
            )
            for r in rows
        ],
        elapsed_ms=elapsed_ms,
    )

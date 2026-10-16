"""Graph data-plane endpoints.

Mounted under ``/v1/{tenant}/graph/...``. Each endpoint is tenant-scoped at
the URL level (so a token with access to one tenant can't read another's
graph).  Every endpoint is plain SQL over AGE's per-label tables, so
``statement_timeout`` and cancellation always apply (AGE's Cypher executor
was seen ignoring both); writes go through ``graphos.writes``.

Core surface (see the module for the full list):

- ``GET  /v1/{tenant}/graph/search``         — substring search by `name`.
- ``POST /v1/{tenant}/graph/nodes``          — small-batch node upsert.
- ``POST /v1/{tenant}/graph/nodes:delete``   — batch node delete, by label and id.
- ``POST /v1/{tenant}/graph/edges``          — small-batch edge upsert.
- ``GET  /v1/{tenant}/graph/neighbors``      — 1-hop in/out edges around a node.
- ``POST /v1/{tenant}/graph/induced_edges``  — edges among a supplied id set.
- ``GET  /v1/{tenant}/graph/catalogue``      — resolver property→labels map.

NDJSON-streaming bulk variants live in ``graphos.api.bulk``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json as _json
import logging
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Any

import asyncpg
from fastapi import (
    APIRouter,
    Body,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse

from graphos import lookup, properties, reserved, writes
from graphos import search as search_index
from graphos.api.jobs import ACCEPTED_RESPONSE, respond_async, run_sync, wants_async
from graphos.auth import ReadPrincipal, Tenant, WritePrincipal, require_tenant
from graphos.bootstrap import registered_tenants
from graphos.contracts.export import (
    DEFAULT_EXPORT_NODE_LIMIT,
    MAX_EXPORT_NODE_LIMIT,
    GraphologyEdge,
    GraphologyExport,
    GraphologyNode,
)
from graphos.contracts.graph import (
    DEFAULT_EXPAND_HOPS,
    DEFAULT_EXPAND_LIMIT,
    DEFAULT_NEIGHBORS_LIMIT,
    DEFAULT_SEARCH_LIMIT,
    MAX_EXPAND_HOPS,
    MAX_EXPAND_LIMIT,
    MAX_NEIGHBORS_LIMIT,
    MAX_NODES_UPSERT_BATCH,
    MAX_SEARCH_LIMIT,
    SCAN_PAYLOAD_CAP_BYTES,
    AggregateRow,
    CatalogueResponse,
    DeleteByPropertyResponse,
    DeleteEdgeResponse,
    DeleteNodeResponse,
    DeleteNodesResponse,
    EdgeGetResponse,
    EdgeHit,
    EdgesAggregateRequest,
    EdgesAggregateResponse,
    EdgesScanItem,
    EdgesScanRequest,
    EdgesScanResponse,
    EdgesUpsertRequest,
    EdgesUpsertResponse,
    EdgeUpsert,
    ExpandResponse,
    InducedEdgesRequest,
    InducedEdgesResponse,
    Neighbor,
    NeighborsResponse,
    NodeAggregateRow,
    NodeGetResponse,
    NodePropertiesUpsertRequest,
    NodePropertiesUpsertResponse,
    NodesAggregateRequest,
    NodesAggregateResponse,
    NodesDeleteRequest,
    NodesScanItem,
    NodesScanRequest,
    NodesScanResponse,
    NodesScanWhere,
    NodesUpsertRequest,
    NodesUpsertResponse,
    NodeUpsert,
    PathHit,
    SampleEdgeHit,
    SampleNodeHit,
    SampleSubgraphRequest,
    SampleSubgraphResponse,
    ScopedSubgraphNode,
    ScopedSubgraphRequest,
    ScopedSubgraphResponse,
    SearchHit,
    SearchResponse,
    _validate_prop_key,
    validate_label_optional,
    validate_property_key,
)
from graphos.contracts.tenants import (
    ResolveEdgesRequest,
    ResolveEdgesResponse,
    ResolveEdgesResult,
)
from graphos.cypher import CypherError, parse_agtype
from graphos.endpoints import policy_for
from graphos.observability.audit import audit_this
from graphos.observability.metrics import record_property_tally
from graphos.resolver import (
    CompileError,
    catalogue_for_properties,
    compile_resolve_rules_for_edge_type,
    get_catalogue,
    rule_property_names,
)

_aggregate_log = logging.getLogger("graphos.aggregate")
_read_log = logging.getLogger("graphos.reads")


def _read_failed(what: str, exc: asyncpg.exceptions.PostgresError) -> HTTPException:
    """A read the database refused: answered as an error (500), never as a
    smaller result, so a client cannot mistake a failure for fewer neighbours.
    The database's message is logged, not sent."""
    _read_log.warning("%s failed: %s: %s", what, type(exc).__name__, str(exc)[:300])
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=(
            f"{what} failed in the database ({exc.sqlstate or type(exc).__name__}); "
            "nothing was left out silently"
        ),
    )

router = APIRouter(tags=["graph"], prefix="/{tenant}/graph")

ExternalId = Annotated[str, Path(description=(
    "The node's `id` (`properties.id`), in any label; it may contain `/`."
))]
EdgeType = Annotated[str, Path(description="The edge type, registered for the tenant.")]


def _safe_decode_props(raw: str | None) -> dict[str, Any]:
    """Decode an agtype-as-text property bag into a Python dict.

    agtype scalars sometimes ship with a trailing ``::agtype`` annotation;
    strip it.  Anything that won't JSON-parse becomes an empty dict so
    the read path stays soft-fallback-friendly.
    """
    if raw is None:
        return {}
    s = raw.rsplit("::", 1)[0] if raw.endswith("::agtype") else raw
    try:
        out = _json.loads(s)
    except (_json.JSONDecodeError, TypeError):
        return {}
    return out if isinstance(out, dict) else {}


def _check_tenant(name: str, principal) -> None:    # type: ignore[no-untyped-def]
    require_tenant(principal, name)
    if name not in registered_tenants():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"tenant '{name}' is not registered. "
                "POST /v1/tenants to create it."
            ),
        )


def _check_vertex_label(tenant: str, label: str | None) -> None:
    if label is not None and label not in registered_tenants()[tenant].vertex_labels:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"vertex label '{label}' is not registered for tenant '{tenant}'",
        )


def _node_ambiguous(external_id: str, exc: lookup.AmbiguousMatchError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"id {external_id!r} exists in labels {exc.candidates}; "
            "pass ?label= to pick one"
        ),
    )


# ── Revisions: ETag / If-Match ─────────────────────────────────────────────

IfMatchHeader = Annotated[
    str | None,
    Header(
        description=(
            "ETag(s) from an earlier read, or `*`: the write happens only if the "
            "node or edge still has one of them (412 otherwise)."
        ),
    ),
]

_PRECONDITION_RESPONSES: dict[int | str, dict[str, Any]] = {
    412: {"description": "`If-Match` did not match: the row changed (or is gone) since read."},
    428: {"description": "`If-Match` is required (`GRAPHOS_REQUIRE_IF_MATCH`) and was absent."},
}


def _if_match(request: Request, header: str | None) -> lookup.IfMatch | None:
    if header is None:
        if request.app.state.settings.require_if_match:
            raise HTTPException(
                status_code=status.HTTP_428_PRECONDITION_REQUIRED,
                detail=(
                    "this write needs an If-Match header with the ETag of a read "
                    "(GRAPHOS_REQUIRE_IF_MATCH)"
                ),
            )
        return None
    return lookup.IfMatch.parse(header)


def _precondition_failed(exc: lookup.PreconditionFailedError) -> HTTPException:
    if exc.current is None:
        return HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="If-Match failed: there is no such node or edge (any more)",
        )
    return HTTPException(
        status_code=status.HTTP_412_PRECONDITION_FAILED,
        detail=f"If-Match failed: changed since it was read; its ETag is now {exc.current}",
        headers={"ETag": exc.current},
    )


def _node_response(
    tenant: str, external_id: str, node: lookup.NodeRow, started: float, response: Response,
) -> NodeGetResponse:
    response.headers["ETag"] = node.etag
    return NodeGetResponse(
        tenant=tenant,
        external_id=external_id,
        label=node.label,
        properties=node.properties,
        rev=node.rev,
        etag=node.etag,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


@router.get(
    "/search",
    response_model=SearchResponse,
    summary="Search nodes: ranked through a search index, or substring on `name`",
    description=(
        "With `index`: ranked search through that named search index (declared at "
        "registration): each hit has a `score` in [0, 1] and the property it `matched`. "
        "An `identifier` index matches exact, prefix, contained and similar words, and with "
        "`fuzzy=true` also misspellings when nothing matches literally; a `text` index is "
        "Postgres full-text search (`\"phrase\"`, `or`, `-word`).\n\n"
        "Without `index`: case-insensitive substring search (`ILIKE`) over `property` "
        "(default `name`), unranked. `name` is served by the trigram index bootstrap "
        "creates; any other property is read row by row until `limit` hits are found, "
        "which is slow on a large tenant when few nodes match. Declare a search index "
        "for properties searched often."
    ),
    responses={404: {"description": "No search index of that name."}},
)
async def search(
    tenant: Tenant,
    request: Request,
    principal: ReadPrincipal,
    q: Annotated[str, Query(min_length=1, max_length=200, description="Substring to match")],
    label: Annotated[
        str | None,
        Query(description="Restrict to one vertex label (UpperCamelCase)."),
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=MAX_SEARCH_LIMIT, description="Max rows to return.")
    ] = DEFAULT_SEARCH_LIMIT,
    index: Annotated[
        str | None, Query(description="A search index of the tenant; ranked search."),
    ] = None,
    fuzzy: Annotated[
        bool, Query(description="`identifier` index: when nothing matches literally, also "
                                "look for misspellings (slower)."),
    ] = False,
    min_score: Annotated[
        float, Query(ge=0, le=1, description="Search index: drop hits scoring lower."),
    ] = 0.0,
    property: Annotated[  # noqa: A002 — the query parameter's name
        str,
        Query(description="Without `index`: the property to match (default `name`)."),
    ] = "name",
) -> SearchResponse:
    _check_tenant(tenant, principal)
    try:
        validate_label_optional(label)
        validate_property_key(property)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    if index is not None:
        return await _ranked_search(
            tenant, request, q=q, label=label, limit=limit, index=index,
            fuzzy=fuzzy, min_score=min_score,
        )

    # SQL-direct rewrite (was: ``MATCH (n) WHERE n.name CONTAINS $q``).
    # The Cypher path planned as a full parent-inheritance scan against
    # every leaf label table — workable at small scale but ~1-2 s on a
    # large tenant (500K+ vertices) because the planner couldn't
    # leverage any index over the ``n.name`` agtype extraction.
    #
    # SQL form: pull the substring match into a single text predicate
    # against ``(properties->'"name"')::text``.  When the consumer has
    # registered a ``gin_trgm_ops`` functional index (see /v1/tenants
    # ``indexes`` field) the planner uses it for sub-50 ms results; when
    # they haven't, falls back to a parallel seq-scan that still beats
    # the agtype-extraction Cypher plan by ~2-5×.
    #
    # ``tableoid -> pg_class`` join echoes the leaf label back so the
    # SearchResponse keeps reporting which label the hit came from
    # (consumer-side UI typically tints by label).  ``id::text`` is the
    # AGE graphid bigint as a string — same shape the Cypher path used
    # to return.
    # A non-None label and the property name were validated above
    # (``validate_label_optional`` / ``validate_property_key``), so both are
    # safe to interpolate.
    from_clause = (
        f'"{tenant}"."_ag_label_vertex" v' if label is None else f'"{tenant}"."{label}" v'
    )

    # JSONB-extract form ``((properties::text)::jsonb)->>'name'`` exactly
    # matches the ``<label>_name_trgm`` GIN index built by
    # :func:`bootstrap.bootstrap_tenant`.  The previous agtype-cast
    # form ``(properties->'"name"')::text`` was a different functional
    # expression that no index could serve — fallback was parallel seq
    # scan across every inheritance child (~1.1 s on a large
    # multi-label inheritance hierarchy, empirically).
    #
    # ``->>`` returns NULL for missing keys, so the explicit
    # ``IS NOT NULL`` guard is redundant — ``ILIKE`` against NULL is
    # NULL which evaluates as false.  Dropping it also unlocks the
    # GIN-only plan (the AND wrapped a non-indexable predicate that
    # forced a Bitmap Heap Scan recheck).
    sql = f"""
        SELECT
          (v.id::text)::bigint                       AS gid,
          c.relname                                  AS label,
          v.properties::text                         AS props_text
        FROM {from_clause}
        JOIN pg_class c ON c.oid = v.tableoid
        WHERE ((((v.properties)::text)::jsonb) ->> '{property}')
              ILIKE '%' || $1 || '%' ESCAPE '!'
        LIMIT $2;
    """

    started = time.perf_counter()
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            # ``q`` is a literal substring: escape LIKE's own wildcards
            # (``!`` is the ESCAPE character, so it is escaped first).
            q_literal = q.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            rows = await conn.fetch(sql, q_literal, limit)
    except asyncpg.exceptions.UndefinedTableError as exc:
        # Tenant graph or label doesn't exist.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="label or tenant not registered",
        ) from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    hits = [
        SearchHit(
            id=int(row["gid"]),
            label=str(row["label"]),
            properties=_safe_decode_props(row["props_text"]),
        )
        for row in rows
    ]

    return SearchResponse(
        tenant=tenant,
        query=q,
        label=label,
        limit=limit,
        truncated=len(hits) == limit,
        elapsed_ms=elapsed_ms,
        nodes=hits,
    )


async def _ranked_search(  # noqa: PLR0913 — the query's options
    tenant: str, request: Request, *, q: str, label: str | None, limit: int,
    index: str, fuzzy: bool, min_score: float,
) -> SearchResponse:
    spec = registered_tenants()[tenant]
    body = spec.search_indexes.get(index)
    if body is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no search index {index!r} in tenant {tenant!r}",
        )
    idx = search_index.SearchIndex.from_spec(index, dict(body))
    if label is not None and not (label in spec.vertex_labels and idx.covers(label)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"search index {index!r} does not cover label {label!r}",
        )
    started = time.perf_counter()
    async with request.app.state.pool.acquire() as conn:
        hits = await search_index.query(
            conn, tenant, idx, q, label=label, limit=limit, fuzzy=fuzzy, min_score=min_score,
        )
    return SearchResponse(
        tenant=tenant,
        query=q,
        label=label,
        index=index,
        limit=limit,
        truncated=len(hits) == limit,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        nodes=[
            SearchHit(id=h.gid, label=h.label, properties=h.properties,
                      score=h.score, matched=h.matched)
            for h in hits
        ],
    )


# ── Upsert ─────────────────────────────────────────────────────────────────


def _group_by_label(nodes: Iterable[NodeUpsert]) -> dict[str, list[dict[str, object]]]:
    """Group payload rows by their label.

    Each label is its own AGE table, so rows are written one label at a time
    through the SQL upsert in ``graphos.cypher``.
    """
    by_label: dict[str, list[dict[str, object]]] = defaultdict(list)
    for n in nodes:
        # `id` is the MERGE key. Properties are merged additively; we strip
        # any caller-supplied `id` in props because it would be redundant
        # (MERGE already writes n.id from the parameter) and might confuse
        # debugging if it ever drifts.
        props = {k: v for k, v in n.properties.items() if k != "id"}
        by_label[n.label].append({"id": n.id, "props": props})
    return by_label


@router.post(
    "/nodes",
    response_model=NodesUpsertResponse,
    summary="Small-batch idempotent node upsert",
    description=(
        "Upsert each node keyed on consumer-assigned `id`. Properties merge "
        "additively (like openCypher `SET n += map`): keys not in the "
        "request payload are preserved. Re-POSTing the same payload is a "
        "graph-state no-op."
    ),
)
async def upsert_nodes(
    tenant: Tenant,
    body: NodesUpsertRequest,
    request: Request,
    principal: WritePrincipal,
) -> NodesUpsertResponse:
    _check_tenant(tenant, principal)

    spec = registered_tenants()[tenant]
    allowed_labels = set(spec.vertex_labels)
    reserved.check(*(n.properties for n in body.nodes))
    tally = properties.enforce(properties.node_rows(properties.policies_for(spec), body.nodes))

    grouped = _group_by_label(body.nodes)
    unknown = sorted(set(grouped) - allowed_labels)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"labels {unknown} are not registered for tenant '{tenant}'. "
                "Re-POST /v1/tenants with the expanded `vertex_labels` list."
            ),
        )

    started = time.perf_counter()
    pool = request.app.state.pool
    by_label_count: dict[str, int] = {}
    total = 0

    for label, rows in grouped.items():
        try:
            n = await writes.upsert_nodes(
                pool, tenant=tenant, label=label, rows=rows,
            )
        except CypherError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"cypher error while upserting :{label}: {exc}",
            ) from exc
        by_label_count[label] = n
        total += n

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    record_property_tally(tenant, "node", tally)
    return NodesUpsertResponse(
        tenant=tenant,
        nodes_upserted=total,
        by_label=by_label_count,
        elapsed_ms=elapsed_ms,
        property_warnings=tally.warnings,
    )



def _row_check(tenant: str, part: str) -> writes.RowCheck | None:
    """The property check for a single write on ``tenant``, or ``None`` when its
    mode is off everywhere."""
    policies = properties.policies_for(registered_tenants()[tenant])
    return properties.checker(policies, part) if policies.any_checks else None

# ── Per-node property merge ────────────────────────────────────────────────
#
# Schema-agnostic primitive that lets any consumer stamp a property bag onto
# an EXISTING node identified by external id — no label needed in the URL.
#
# One generic surface for any metadata-bearing node (``:Team``,
# ``:Collection``, or anything a consumer schema defines).  Composing
# aggregates is the consumer's concern; graphos just owns "merge these keys
# onto that node."


@router.get(
    "/nodes/{external_id:path}",
    response_model=NodeGetResponse,
    summary="Fetch one node by external id",
    description=(
        "Returns the full property bag of the node whose "
        "``properties.id`` matches ``external_id``, in any label (served by "
        "each label's ``*_id_unique`` btree).  An id that exists in several "
        "labels answers 409 unless ``label`` picks one."
    ),
    responses={409: {"description": "The id exists in several labels; pass `label`."}},
)
async def get_node_by_external_id(  # noqa: PLR0913 — FastAPI route parameters
    tenant: Tenant,
    external_id: ExternalId,
    request: Request,
    response: Response,
    principal: ReadPrincipal,
    label: Annotated[
        str | None,
        Query(description="The node's label; needed only when the id exists in several labels."),
    ] = None,
) -> NodeGetResponse:
    _check_tenant(tenant, principal)
    _check_vertex_label(tenant, label)
    started = time.perf_counter()
    async with request.app.state.pool.acquire() as conn:
        try:
            node = await lookup.find_node(
                conn, tenant=tenant, external_id=external_id, label=label,
            )
        except lookup.AmbiguousMatchError as exc:
            raise _node_ambiguous(external_id, exc) from exc
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no node with id={external_id!r} in tenant {tenant!r}",
        )
    return _node_response(tenant, external_id, node, started, response)


@router.patch(
    "/nodes/{external_id:path}",
    response_model=NodeGetResponse,
    summary="Patch one node's properties (JSON Merge Patch)",
    description=(
        "Applies the body as a JSON Merge Patch (RFC 7396, `Content-Type: "
        "application/merge-patch+json` or `application/json`) to the "
        "properties of the node whose `id` is `external_id`: a key set to "
        "`null` is removed, a nested object is merged key by key, any other "
        "value replaces the old one, keys not in the patch are kept.  `id` "
        "names the node and cannot be patched.  Returns the node as written, "
        "with its new `ETag`."
    ),
    responses={
        404: {"description": "No node has that id."},
        409: {"description": "The id exists in several labels; pass `label`."},
        **_PRECONDITION_RESPONSES,
    },
)
async def patch_node(  # noqa: PLR0913 — FastAPI route parameters
    tenant: Tenant,
    external_id: ExternalId,
    request: Request,
    response: Response,
    principal: WritePrincipal,
    patch: Annotated[
        dict[str, Any],
        Body(media_type="application/merge-patch+json", description="JSON Merge Patch."),
    ],
    label: Annotated[
        str | None,
        Query(description="The node's label; needed only when the id exists in several labels."),
    ] = None,
    if_match: IfMatchHeader = None,
) -> NodeGetResponse:
    _check_tenant(tenant, principal)
    _check_vertex_label(tenant, label)
    reserved.check(patch)
    precondition = _if_match(request, if_match)
    started = time.perf_counter()
    try:
        node = await writes.patch_node(
            request.app.state.pool, tenant=tenant, external_id=external_id,
            patch=patch, label=label, if_match=precondition, check=_row_check(tenant, "vertex"),
        )
    except lookup.PreconditionFailedError as exc:
        raise _precondition_failed(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc),
        ) from exc
    except lookup.AmbiguousMatchError as exc:
        raise _node_ambiguous(external_id, exc) from exc
    except CypherError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"patch failed: {exc}",
        ) from exc
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no node with id={external_id!r} in tenant {tenant!r}",
        )
    return _node_response(tenant, external_id, node, started, response)


@router.post(
    "/nodes/{external_id:path}/properties",
    response_model=NodePropertiesUpsertResponse,
    summary="Merge a property bag onto the node identified by external id",
    description=(
        "Idempotent, last-write-wins merge of the supplied property bag onto "
        "the node whose ``properties.id`` matches ``external_id``.  Keys not "
        "in the payload are preserved; keys in the payload overwrite their "
        "previous value at the top level (agtype ``||`` semantics).\n\n"
        "Designed for stamping aggregates / materialised summaries onto "
        "metadata-bearing nodes (e.g. ``:Team`` for team-wide stats, "
        "``:Collection`` for per-collection document counts).  No coupling to any "
        "specific schema — every consumer brings its own label conventions.\n\n"
        "An id that exists in several labels answers 409 unless ``label`` "
        "picks one; nothing is written."
    ),
    responses={
        409: {"description": "The id exists in several labels; pass `label`."},
        **_PRECONDITION_RESPONSES,
    },
)
async def upsert_node_properties(  # noqa: PLR0913 — FastAPI route parameters
    tenant: Tenant,
    external_id: ExternalId,
    body: NodePropertiesUpsertRequest,
    request: Request,
    response: Response,
    principal: WritePrincipal,
    label: Annotated[
        str | None,
        Query(description="The node's label; needed only when the id exists in several labels."),
    ] = None,
    if_match: IfMatchHeader = None,
) -> NodePropertiesUpsertResponse:
    _check_tenant(tenant, principal)
    _check_vertex_label(tenant, label)
    reserved.check(body.properties)
    precondition = _if_match(request, if_match)
    started = time.perf_counter()
    pool = request.app.state.pool

    try:
        node = await writes.merge_node_properties(
            pool, tenant=tenant, external_id=external_id, properties=body.properties,
            label=label, if_match=precondition, check=_row_check(tenant, "vertex"),
        )
    except lookup.PreconditionFailedError as exc:
        raise _precondition_failed(exc) from exc
    except lookup.AmbiguousMatchError as exc:
        raise _node_ambiguous(external_id, exc) from exc
    if node is None:
        # Node not found — distinguish from "found but unchanged" because
        # the caller may have a stale view of which nodes exist.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no node with id={external_id!r} in tenant {tenant!r} "
                f"(register the parent label or upsert the node first)."
            ),
        )

    response.headers["ETag"] = node.etag
    return NodePropertiesUpsertResponse(
        tenant=tenant,
        external_id=external_id,
        label=node.label,
        properties_merged=len(body.properties),
        rev=node.rev,
        etag=node.etag,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


# ── Edge upsert ────────────────────────────────────────────────────────────


EdgeKey = tuple[str, str, str]


def _group_edges(edges: Iterable[EdgeUpsert]) -> dict[EdgeKey, list[dict[str, object]]]:
    """Group payload rows by ``(src_label, dst_label, type)``.

    Like nodes, edges can't parameterise the label/type binders in the
    MATCH/MERGE so we issue one Cypher template per distinct triple.
    """
    grouped: dict[EdgeKey, list[dict[str, object]]] = defaultdict(list)
    for e in edges:
        grouped[(e.src_label, e.dst_label, e.type)].append(
            {"src_id": e.src_id, "dst_id": e.dst_id, "props": dict(e.properties)}
        )
    return grouped


@router.post(
    "/edges",
    response_model=EdgesUpsertResponse,
    summary="Small-batch idempotent edge upsert",
    description=(
        "MERGE each edge keyed on (src_id, dst_id, type). Both endpoints "
        "must already exist as nodes — if either is missing the edge is "
        "skipped and counted in `edges_skipped` (graphos does not "
        "auto-create endpoints). Properties merge additively."
    ),
)
async def upsert_edges(
    tenant: Tenant,
    body: EdgesUpsertRequest,
    request: Request,
    principal: WritePrincipal,
) -> EdgesUpsertResponse:
    _check_tenant(tenant, principal)

    spec = registered_tenants()[tenant]
    allowed_v = set(spec.vertex_labels)
    allowed_e = set(spec.edge_labels)
    reserved.check(*(e.properties for e in body.edges))
    tally = properties.enforce(properties.edge_rows(properties.policies_for(spec), body.edges))

    grouped = _group_edges(body.edges)

    unknown_v: set[str] = set()
    unknown_e: set[str] = set()
    for (src_lbl, dst_lbl, et) in grouped:
        if src_lbl not in allowed_v:
            unknown_v.add(src_lbl)
        if dst_lbl not in allowed_v:
            unknown_v.add(dst_lbl)
        if et not in allowed_e:
            unknown_e.add(et)
    if unknown_v or unknown_e:
        msg = []
        if unknown_v:
            msg.append(f"vertex labels {sorted(unknown_v)} not registered")
        if unknown_e:
            msg.append(f"edge labels {sorted(unknown_e)} not registered")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="; ".join(msg) + ". Re-POST /v1/tenants with the expanded labels.",
        )
    # With strict_endpoints, a declared type only joins its declared label pairs.
    policy = policy_for(spec)
    refusals = [
        why for (src_lbl, dst_lbl, et) in grouped
        if (why := policy.refusal(et, src_lbl, dst_lbl)) is not None
    ]
    if refusals:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=" ".join(refusals))

    started = time.perf_counter()
    pool = request.app.state.pool
    by_type: dict[str, int] = {}
    total_merged = 0
    total_skipped = 0

    for (src_lbl, dst_lbl, et), rows in grouped.items():
        try:
            merged, skipped = await writes.upsert_edges(
                pool,
                tenant=tenant,
                src_label=src_lbl,
                dst_label=dst_lbl,
                edge_type=et,
                rows=rows,
            )
        except CypherError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"cypher error on -[{src_lbl}-{et}->{dst_lbl}]-: {exc}"
                ),
            ) from exc

        by_type[et] = by_type.get(et, 0) + merged
        total_merged += merged
        total_skipped += skipped

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    record_property_tally(tenant, "edge", tally)
    return EdgesUpsertResponse(
        tenant=tenant,
        edges_upserted=total_merged,
        edges_skipped=total_skipped,
        by_type=by_type,
        elapsed_ms=elapsed_ms,
        property_warnings=tally.warnings,
    )


# ── One edge, by identity ──────────────────────────────────────────────────
#
# An edge is named by its type and the ids of its two ends: there is at most
# one edge of a type between two vertices.  An end id that exists in several
# labels can make that more than one edge; the end labels then pick one.


@dataclass(frozen=True, slots=True)
class _EdgeRef:
    src: str
    dst: str
    src_label: str | None
    dst_label: str | None


def _edge_ref(
    src: Annotated[str, Query(min_length=1, description="`id` of the source node.")],
    dst: Annotated[str, Query(min_length=1, description="`id` of the target node.")],
    src_label: Annotated[
        str | None, Query(description="Source label; needed only when the id is in several."),
    ] = None,
    dst_label: Annotated[
        str | None, Query(description="Target label; needed only when the id is in several."),
    ] = None,
) -> _EdgeRef:
    return _EdgeRef(src, dst, src_label, dst_label)


EdgeRef = Annotated[_EdgeRef, Depends(_edge_ref)]

_EDGE_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"description": "Unregistered edge type, or no such edge."},
    409: {"description": "Several edges match; pass `src_label` / `dst_label`."},
}


def _check_edge_ref(tenant: str, edge_type: str, ref: _EdgeRef) -> None:
    if edge_type not in registered_tenants()[tenant].edge_labels:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"edge type '{edge_type}' is not registered for tenant '{tenant}'",
        )
    _check_vertex_label(tenant, ref.src_label)
    _check_vertex_label(tenant, ref.dst_label)


def _edge_ambiguous(edge_type: str, exc: lookup.AmbiguousMatchError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"{len(exc.candidates)} {edge_type} edges match, between labels "
            f"{exc.candidates}; pass src_label and/or dst_label to pick one"
        ),
    )


def _no_edge(tenant: str, edge_type: str, ref: _EdgeRef) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"no {edge_type} edge from {ref.src!r} to {ref.dst!r} in tenant {tenant!r}",
    )


def _edge_response(  # noqa: PLR0913 — everything the response echoes
    tenant: str, edge_type: str, ref: _EdgeRef, edge: lookup.EdgeRow, started: float,
    response: Response,
) -> EdgeGetResponse:
    response.headers["ETag"] = edge.etag
    return EdgeGetResponse(
        tenant=tenant,
        type=edge_type,
        src_id=ref.src,
        src_label=edge.src_label,
        dst_id=ref.dst,
        dst_label=edge.dst_label,
        properties=edge.properties,
        rev=edge.rev,
        etag=edge.etag,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


@router.get(
    "/edges/{edge_type}",
    response_model=EdgeGetResponse,
    summary="Fetch one edge by its type and the ids of its ends",
    description=(
        "An edge is named by its type and the ids of its two ends: there is at most one edge "
        "of a type between two nodes. When an end id exists in several labels more than one "
        "edge can match (409); `src_label` / `dst_label` pick one. Returns its properties, "
        "the labels of its ends and its revision, with its `ETag` for a later `If-Match`."
    ),
    responses=_EDGE_RESPONSES,
)
async def get_edge(  # noqa: PLR0913 — FastAPI route parameters
    tenant: Tenant,
    edge_type: EdgeType,
    ref: EdgeRef,
    request: Request,
    response: Response,
    principal: ReadPrincipal,
) -> EdgeGetResponse:
    _check_tenant(tenant, principal)
    _check_edge_ref(tenant, edge_type, ref)
    started = time.perf_counter()
    async with request.app.state.pool.acquire() as conn:
        try:
            edge = await lookup.find_edge(
                conn, tenant=tenant, edge_type=edge_type, src_id=ref.src, dst_id=ref.dst,
                src_label=ref.src_label, dst_label=ref.dst_label,
            )
        except lookup.AmbiguousMatchError as exc:
            raise _edge_ambiguous(edge_type, exc) from exc
    if edge is None:
        raise _no_edge(tenant, edge_type, ref)
    return _edge_response(tenant, edge_type, ref, edge, started, response)


@router.patch(
    "/edges/{edge_type}",
    response_model=EdgeGetResponse,
    summary="Patch one edge's properties (JSON Merge Patch)",
    description=(
        "Applies the body as a JSON Merge Patch (RFC 7396, `Content-Type: "
        "application/merge-patch+json` or `application/json`) to the "
        "properties of one edge: a key set to `null` is removed, a nested "
        "object is merged key by key, any other value replaces the old one, "
        "keys not in the patch are kept.  Returns the edge as written, with its "
        "new `ETag`."
    ),
    responses={**_EDGE_RESPONSES, **_PRECONDITION_RESPONSES},
)
async def patch_edge(  # noqa: PLR0913 — FastAPI route parameters
    tenant: Tenant,
    edge_type: EdgeType,
    ref: EdgeRef,
    request: Request,
    response: Response,
    principal: WritePrincipal,
    patch: Annotated[
        dict[str, Any],
        Body(media_type="application/merge-patch+json", description="JSON Merge Patch."),
    ],
    if_match: IfMatchHeader = None,
) -> EdgeGetResponse:
    _check_tenant(tenant, principal)
    _check_edge_ref(tenant, edge_type, ref)
    reserved.check(patch)
    precondition = _if_match(request, if_match)
    started = time.perf_counter()
    try:
        edge = await writes.patch_edge(
            request.app.state.pool, tenant=tenant, edge_type=edge_type,
            src_id=ref.src, dst_id=ref.dst, patch=patch,
            src_label=ref.src_label, dst_label=ref.dst_label, if_match=precondition,
            check=_row_check(tenant, "edge"),
        )
    except lookup.PreconditionFailedError as exc:
        raise _precondition_failed(exc) from exc
    except lookup.AmbiguousMatchError as exc:
        raise _edge_ambiguous(edge_type, exc) from exc
    except CypherError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"patch failed: {exc}",
        ) from exc
    if edge is None:
        raise _no_edge(tenant, edge_type, ref)
    return _edge_response(tenant, edge_type, ref, edge, started, response)


@router.delete(
    "/edges/{edge_type}",
    response_model=DeleteEdgeResponse,
    summary="Delete one edge; its end nodes stay",
    description=(
        "Idempotent: deleting an edge that does not exist returns `edges_deleted: 0` "
        "(with `If-Match`, 412)."
    ),
    responses={
        404: {"description": "Unregistered edge type."},
        409: _EDGE_RESPONSES[409],
        **_PRECONDITION_RESPONSES,
    },
)
async def delete_edge(  # noqa: PLR0913 — FastAPI route parameters
    tenant: Tenant,
    edge_type: EdgeType,
    ref: EdgeRef,
    request: Request,
    principal: WritePrincipal,
    if_match: IfMatchHeader = None,
) -> DeleteEdgeResponse:
    _check_tenant(tenant, principal)
    _check_edge_ref(tenant, edge_type, ref)
    precondition = _if_match(request, if_match)
    started = time.perf_counter()
    try:
        deleted = await writes.delete_edge(
            request.app.state.pool, tenant=tenant, edge_type=edge_type,
            src_id=ref.src, dst_id=ref.dst,
            src_label=ref.src_label, dst_label=ref.dst_label, if_match=precondition,
        )
    except lookup.PreconditionFailedError as exc:
        raise _precondition_failed(exc) from exc
    except lookup.AmbiguousMatchError as exc:
        raise _edge_ambiguous(edge_type, exc) from exc
    except CypherError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"delete failed: {exc}",
        ) from exc
    return DeleteEdgeResponse(
        tenant=tenant,
        type=edge_type,
        src_id=ref.src,
        dst_id=ref.dst,
        edges_deleted=deleted,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


# ── Neighbors ──────────────────────────────────────────────────────────────


@router.get(
    "/neighbors",
    response_model=NeighborsResponse,
    summary="1-hop neighbors of a node (in + out edges, optionally filtered by type)",
    description=(
        "Returns up to `limit` neighbors. For each neighbor row we return "
        "the neighbor's id/label/properties plus the edge that connects "
        "them, with `direction` indicating which way the edge points "
        "relative to the requested node."
    ),
)
async def neighbors(
    tenant: Tenant,
    request: Request,
    principal: ReadPrincipal,
    node_id: Annotated[
        str,
        Query(min_length=1, description="Consumer-assigned id of the seed node."),
    ],
    rel_types: Annotated[
        list[str] | None,
        Query(description="If set, restrict to edges of these types."),
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=MAX_NEIGHBORS_LIMIT, description="Max neighbors to return."),
    ] = DEFAULT_NEIGHBORS_LIMIT,
) -> NeighborsResponse:
    _check_tenant(tenant, principal)

    # Validate optional rel-type filter list.
    if rel_types is not None:
        for rt in rel_types:
            try:
                validate_label_optional(rt)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ) from exc

    # AGE Cypher MATCH (n {id: X})-[r]->(m) chokes at 4 M-edge scale --
    # the planner joins across every child label table looking for the
    # source.  Equivalent raw SQL on the per-label storage tables uses
    # the btree(start_id, end_id) we install at bootstrap and finishes
    # in milliseconds.  Same SQL-direct shape as sample_subgraph.
    started = time.perf_counter()
    pool = request.app.state.pool
    neighs: list[Neighbor] = []

    async with pool.acquire() as conn:
        # ── 1. Resolve source graphid from external id (uses the GIN
        #      index on properties; scans the inheritance parent so
        #      we don't need to know the source label up-front).
        src_row = await conn.fetchrow(
            f'SELECT id::text AS gid FROM "{tenant}"."_ag_label_vertex" '
            f'WHERE properties @> $1::agtype LIMIT 1;',
            _json.dumps({"id": node_id}),
        )
        if src_row is None:
            return NeighborsResponse(
                tenant=tenant, node_id=node_id, limit=limit,
                truncated=False,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                neighbors=[],
            )
        src_gid = src_row["gid"]

        # ── 2. Discover edge-label tables (or use caller's whitelist).
        if rel_types:
            edge_labels = list(rel_types)
        else:
            edge_label_rows = await conn.fetch(
                "SELECT c.relname AS label "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_inherits i ON i.inhrelid = c.oid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE n.nspname = $1 AND c.relkind = 'r' "
                "AND p.relname = '_ag_label_edge' "
                "ORDER BY c.relname;",
                tenant,
            )
            edge_labels = [r["label"] for r in edge_label_rows]

        # ── 3. Outgoing + incoming edges via the AGE parent edge
        #      inheritance table.  Postgres routes the scan to each
        #      child label that matches the WHERE/relname filter,
        #      each using its own btree(start_id, end_id) index.
        #      One query per direction instead of one per rel_type.
        safe_rels: list[str] = []
        for rel in edge_labels:
            try:
                validate_label_optional(rel)
                safe_rels.append(rel)
            except ValueError:
                continue
        # ``IN (NULL)`` matches nothing: an empty rel list must not render ``IN ()``.
        rel_in = ", ".join(f"'{r}'" for r in safe_rels) or "NULL"
        endpoints: list[tuple[str, str, str, dict[str, Any]]] = []
        # (direction, rel_type, other_gid, edge_props)
        try:
            out_rows = await conn.fetch(
                f'SELECT c.relname AS rel, '
                f'       e.end_id::text AS gid, '
                f'       e.properties::text AS props '
                f'FROM "{tenant}"."_ag_label_edge" e '
                f'JOIN pg_class c ON c.oid = e.tableoid '
                f'WHERE e.start_id = $1::graphid '
                f'  AND c.relname IN ({rel_in}) '
                f'ORDER BY e.end_id, c.relname '
                f'LIMIT $2;',
                src_gid, limit,
            )
            in_rows = await conn.fetch(
                f'SELECT c.relname AS rel, '
                f'       e.start_id::text AS gid, '
                f'       e.properties::text AS props '
                f'FROM "{tenant}"."_ag_label_edge" e '
                f'JOIN pg_class c ON c.oid = e.tableoid '
                f'WHERE e.end_id = $1::graphid '
                f'  AND c.relname IN ({rel_in}) '
                f'ORDER BY e.start_id, c.relname '
                f'LIMIT $2;',
                src_gid, limit,
            )
        except asyncpg.exceptions.QueryCanceledError:
            raise
        except asyncpg.exceptions.PostgresError as exc:
            raise _read_failed("neighbors", exc) from exc
        for r in out_rows:
            if len(endpoints) >= limit:
                break
            endpoints.append(
                ("out", str(r["rel"]), str(r["gid"]),
                 _safe_decode_props(r["props"])),
            )
        for r in in_rows:
            if len(endpoints) >= limit:
                break
            endpoints.append(
                ("in", str(r["rel"]), str(r["gid"]),
                 _safe_decode_props(r["props"])),
            )

        # ── 4. Bulk-resolve neighbor properties + labels via
        #      tableoid->pg_class join on the parent vertex table.
        other_ids = list({ep[2] for ep in endpoints})
        gid_to_meta: dict[str, tuple[str, dict[str, Any]]] = {}
        if other_ids:
            id_csv = ", ".join(f"'{i}'::graphid" for i in other_ids)
            vrows = await conn.fetch(
                f'SELECT v.id::text AS gid, '
                f'       v.properties::text AS props, '
                f'       c.relname AS lbl '
                f'FROM "{tenant}"."_ag_label_vertex" v '
                f'JOIN pg_class c ON c.oid = v.tableoid '
                f'WHERE v.id IN ({id_csv});',
            )
            for vr in vrows:
                gid_to_meta[str(vr["gid"])] = (
                    str(vr["lbl"]), _safe_decode_props(vr["props"]),
                )

        for direction, rel, other_gid, edge_props in endpoints:
            lbl, n_props = gid_to_meta.get(other_gid, ("", {}))
            ext_id = str(n_props.get("id") or other_gid)
            neighs.append(
                Neighbor(
                    neighbor_id=ext_id,
                    neighbor_label=lbl,
                    neighbor_properties=n_props,
                    direction=direction,
                    edge_type=rel,
                    edge_properties=edge_props,
                )
            )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    # Per-rel queries already capped at the remaining budget; we
    # never exceed ``limit`` in the loop, but keep a defensive slice
    # for safety.
    if len(neighs) > limit:
        neighs = neighs[:limit]

    return NeighborsResponse(
        tenant=tenant,
        node_id=node_id,
        limit=limit,
        truncated=len(neighs) == limit,
        elapsed_ms=elapsed_ms,
        neighbors=neighs,
    )


# ── Induced edges ──────────────────────────────────────────────────────────


@router.post(
    "/induced_edges",
    response_model=InducedEdgesResponse,
    summary="Edges whose both endpoints are in a supplied id set",
    description=(
        "Returns every edge where `src.id` and `dst.id` are both in "
        "`node_ids`. Optional `rel_types` narrows by edge type. Useful "
        "for getting the subgraph induced by a ranked node set."
    ),
)
async def induced_edges(
    tenant: Tenant,
    body: InducedEdgesRequest,
    request: Request,
    principal: ReadPrincipal,
) -> InducedEdgesResponse:
    _check_tenant(tenant, principal)

    if body.rel_types is not None:
        for rt in body.rel_types:
            try:
                validate_label_optional(rt)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ) from exc

    # SQL-direct rewrite (was: ``MATCH (a)-[r]->(b) WHERE a.id IN $ids
    # AND b.id IN $ids``).  The Cypher path walked the full edge
    # inheritance tree and filtered each row through the agtype list
    # membership — ~3-5 s on a 6 M-edge tenant with a 50-id set.
    #
    # SQL form: resolve the external id list to AGE graphids ONCE via
    # the per-label ``*_id_unique`` btrees, then probe the edge tables
    # with both endpoints in the resolved gid set.  Two indexed lookups
    # per edge probe instead of a property scan.
    #
    # 1. Resolve external ids → graphid set.
    started = time.perf_counter()
    pool = request.app.state.pool

    # Build the agtype id set the same way community-ids does so the
    # ``(properties->'"id"') = ANY (...)`` predicate matches the
    # indexed expression.
    unique_ids = list({i for i in body.node_ids if i})
    if not unique_ids:
        return InducedEdgesResponse(
            tenant=tenant, limit=body.limit, truncated=False,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            edges=[],
        )

    async with pool.acquire() as conn:
        gid_rows = await conn.fetch(
            f"""
            SELECT
              v.id::text                                            AS gid,
              trim(both '"' from (v.properties->'"id"')::text)      AS ext_id
            FROM "{tenant}"."_ag_label_vertex" v
            WHERE (v.properties->'"id"') = ANY(
              ARRAY(
                SELECT ('"' || t || '"')::agtype
                FROM unnest($1::text[]) AS t
              )
            );
            """,
            unique_ids,
        )
        gid_by_ext: dict[str, int] = {
            str(r["ext_id"]): int(r["gid"]) for r in gid_rows
        }
        if not gid_by_ext:
            return InducedEdgesResponse(
                tenant=tenant, limit=body.limit, truncated=False,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                edges=[],
            )
        ext_by_gid = {v: k for k, v in gid_by_ext.items()}
        gid_set = list(gid_by_ext.values())

        # 2. Probe edges with both endpoints in the resolved gid set.
        #    The per-label ``(start_id, end_id)`` btrees are on the raw
        #    ``graphid`` type — matching ``e.start_id::text::bigint``
        #    (the previous shape) defeats them via the cast.  We pass
        #    the gids as text and convert to ``graphid[]`` in SQL via
        #    ``unnest(...)``, so the WHERE predicate matches the
        #    indexed column form and the planner picks the btrees.
        gid_text = [str(g) for g in gid_set]
        if body.rel_types:
            rel_filter_sql = "AND c.relname = ANY($3::text[])"
            params = (gid_text, body.limit, list(body.rel_types))
        else:
            rel_filter_sql = ""
            params = (gid_text, body.limit)

        edge_rows = await conn.fetch(
            f"""
            WITH ids AS (
              SELECT t::graphid AS gid FROM unnest($1::text[]) AS t
            )
            SELECT
              (e.id::text)::bigint        AS rid,
              (e.start_id::text)::bigint  AS start_gid,
              (e.end_id::text)::bigint    AS end_gid,
              c.relname                   AS rel_type,
              e.properties::text          AS rel_props_text
            FROM "{tenant}"."_ag_label_edge" e
            JOIN pg_class c ON c.oid = e.tableoid
            WHERE e.start_id IN (SELECT gid FROM ids)
              AND e.end_id   IN (SELECT gid FROM ids)
              {rel_filter_sql}
            LIMIT $2;
            """,
            *params,
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    edges = []
    for r in edge_rows:
        # Map AGE graphids back to caller-facing external ids.  We
        # restrict the response to edges whose BOTH endpoints resolved —
        # the SQL filter guarantees this by construction, but the dict
        # lookups defend against any future edge inheritance widening
        # the result set.
        src_ext = ext_by_gid.get(int(r["start_gid"]))
        dst_ext = ext_by_gid.get(int(r["end_gid"]))
        if src_ext is None or dst_ext is None:
            continue
        edges.append(EdgeHit(
            id=int(r["rid"]),
            src_id=src_ext,
            dst_id=dst_ext,
            type=str(r["rel_type"]),
            properties=_safe_decode_props(r["rel_props_text"]),
        ))

    return InducedEdgesResponse(
        tenant=tenant,
        node_count=len(body.node_ids),
        rel_types=list(body.rel_types) if body.rel_types else None,
        limit=body.limit,
        truncated=len(edges) == body.limit,
        elapsed_ms=elapsed_ms,
        edges=edges,
    )


# ── Scoped subgraph ────────────────────────────────────────────────────────


def _agtype_literal(value: str) -> str:
    """Quote ``value`` as an agtype string literal for indexed equality."""
    return "'" + _json.dumps(value).replace("'", "''") + "'::agtype"


def _agtype_scalar(raw: str | None) -> object:
    """Decode one ``(properties->'"k"')::text`` value into a Python scalar."""
    if raw is None:
        return None
    text = raw
    for suffix in ("::numeric", "::float", "::integer"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    try:
        return _json.loads(text)
    except ValueError:
        return text


@router.post(
    "/scoped_subgraph",
    response_model=ScopedSubgraphResponse,
    summary="Subgraph of the vertices sharing one property value",
    description=(
        "Vertices whose `scope_property` equals `scope_value` (optionally "
        "restricted to `group_values` of `group_property` and balanced "
        "across them, capped at `node_cap`) plus the induced edges among "
        "them. Every property name is supplied by the caller. One direct-"
        "SQL scan over the scope property; index it for large tenants."
    ),
)
async def scoped_subgraph(
    tenant: Tenant,
    body: ScopedSubgraphRequest,
    request: Request,
    principal: ReadPrincipal,
) -> ScopedSubgraphResponse:
    _check_tenant(tenant, principal)
    started = time.perf_counter()
    pool = request.app.state.pool

    # Properties to read per vertex: what the caller asked to see, plus
    # whatever the grouping / exclusion logic needs.  All names passed the
    # identifier regex in the contract, so they are safe to interpolate.
    wanted: list[str] = list(dict.fromkeys(
        [*body.include_properties,
         *([body.group_property] if body.group_property else []),
         *([body.exclude_property] if body.exclude_property else [])]
    ))
    prop_cols = "".join(
        f", (v.properties->'\"{k}\"')::text AS p{i}" for i, k in enumerate(wanted)
    )
    group_sql = ""
    groups = [g for g in (body.group_values or []) if g]
    if body.group_property and groups:
        arr = "ARRAY[" + ", ".join(_agtype_literal(g) for g in groups) + "]::agtype[]"
        group_sql = f"AND (v.properties->'\"{body.group_property}\"') = ANY({arr}) "
    per_group_cap = max(40, body.node_cap // max(len(groups), 1))

    # One scan filtered by the scope property (and group membership),
    # over-fetched so the Python pass below can balance and exclude.
    over_fetch = min(max(body.node_cap * 4, 2000), 8000)
    node_sql = (
        f"SELECT v.id::text AS gid, "
        f"  trim(both '\"' from (v.properties->'\"id\"')::text) AS ext_id, "
        f"  c.relname AS label{prop_cols} "
        f'FROM "{tenant}"."_ag_label_vertex" v '
        f"JOIN pg_class c ON c.oid = v.tableoid "
        f"WHERE (v.properties->'\"{body.scope_property}\"') = "
        f"{_agtype_literal(body.scope_value)} "
        f"{group_sql}"
        f"LIMIT {over_fetch};"
    )
    markers = [m.lower() for m in (body.exclude_substrings or []) if m]
    include = set(body.include_properties)

    async with pool.acquire() as conn:
        node_rows = await conn.fetch(node_sql)

        nodes: list[ScopedSubgraphNode] = []
        gids: list[str] = []
        ext_by_gid: dict[int, str] = {}
        seen: set[str] = set()
        per_group_count: dict[str, int] = {}
        for r in node_rows:
            if len(nodes) >= body.node_cap:
                break
            props = {k: _agtype_scalar(r[f"p{i}"]) for i, k in enumerate(wanted)}
            if markers:
                probe = str(props.get(body.exclude_property) or "").lower()
                if any(m in probe for m in markers):
                    continue
            if groups:
                g = str(props.get(body.group_property) or "")
                if per_group_count.get(g, 0) >= per_group_cap:
                    continue
                per_group_count[g] = per_group_count.get(g, 0) + 1
            ext = r["ext_id"] or r["gid"]
            if not ext or ext in seen:
                continue
            seen.add(ext)
            gids.append(str(r["gid"]))
            ext_by_gid[int(r["gid"])] = ext
            nodes.append(ScopedSubgraphNode(
                id=ext, label=r["label"] or "",
                properties={k: v for k, v in props.items() if k in include and v is not None},
            ))
        truncated = len(nodes) >= body.node_cap or len(node_rows) >= over_fetch

        edges: list[EdgeHit] = []
        if gids:
            if body.rel_types:
                rel_sql = "AND c.relname = ANY($2::text[])"
                edge_params = (gids, list(body.rel_types), body.edge_limit)
                limit_ph = "$3"
            else:
                rel_sql = ""
                edge_params = (gids, body.edge_limit)
                limit_ph = "$2"
            edge_rows = await conn.fetch(
                f"""
                WITH ids AS (
                  SELECT t::graphid AS gid FROM unnest($1::text[]) AS t
                )
                SELECT (e.id::text)::bigint AS rid,
                       (e.start_id::text)::bigint AS start_gid,
                       (e.end_id::text)::bigint   AS end_gid,
                       c.relname AS rel_type
                FROM "{tenant}"."_ag_label_edge" e
                JOIN pg_class c ON c.oid = e.tableoid
                WHERE e.start_id IN (SELECT gid FROM ids)
                  AND e.end_id   IN (SELECT gid FROM ids)
                  {rel_sql}
                LIMIT {limit_ph};
                """,
                *edge_params,
            )
            for r in edge_rows:
                s = ext_by_gid.get(int(r["start_gid"]))
                d = ext_by_gid.get(int(r["end_gid"]))
                if s is None or d is None:
                    continue
                edges.append(EdgeHit(
                    id=int(r["rid"]), src_id=s, dst_id=d,
                    type=str(r["rel_type"]),
                ))

    return ScopedSubgraphResponse(
        tenant=tenant, scope_property=body.scope_property,
        scope_value=body.scope_value, truncated=truncated,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        nodes=nodes, edges=edges,
    )


# ── Catalogue (resolver diagnostic) ────────────────────────────────────────


@router.get(
    "/catalogue",
    response_model=CatalogueResponse,
    summary="Resolver property→labels catalogue for this tenant",
    description=(
        "Returns the in-process map the resolver compiler consults: "
        "for each top-level JSONB property name observed on at least "
        "one vertex row, the list of leaf labels carrying it "
        "(inheritance parents pruned).  Use this to verify that the "
        "property names in a registered ``resolve_rule`` actually "
        "resolve to label tables before firing ``/edges:resolve``.\n\n"
        "The map is kept as nodes are written, so it is current after "
        "every commit.  ``property`` (repeatable) narrows it to those "
        "properties, as ``edges:resolve`` does.  A property no node carries "
        "any more may still be listed; ``refresh=true`` re-reads every node "
        "to drop such names (slow on a large tenant)."
    ),
)
async def catalogue(
    tenant: Tenant,
    request: Request,
    principal: ReadPrincipal,
    refresh: Annotated[
        bool,
        Query(description="Re-read every node first, dropping properties no node carries."),
    ] = False,
    property: Annotated[  # noqa: A002 — the query parameter's name
        list[str] | None,
        Query(description="Only these properties."),
    ] = None,
) -> CatalogueResponse:
    _check_tenant(tenant, principal)
    pool = request.app.state.pool
    cat = (
        await catalogue_for_properties(pool, tenant, property)
        if property
        else await get_catalogue(pool, tenant, refresh=refresh)
    )
    return CatalogueResponse(
        tenant=tenant,
        by_property={
            prop: sorted(labels) for prop, labels in cat.by_property.items()
        },
    )


# ── Expand (variable-length BFS) ────────────────────────────────────────────


@router.get(
    "/expand",
    response_model=ExpandResponse,
    summary="Variable-length BFS expand from a seed node",
    description=(
        "Returns endpoints reachable within `max_hops` hops from `node_id`, "
        "ordered by hop count ascending. Each result carries the path's "
        "edge-type sequence. Direction `out`/`in`/`both` controls edge "
        "orientation. Maximum hops is capped to keep query cost bounded; "
        "consumers that need deeper traversal should issue iterative "
        "expansions or — if the use case is permanent — request a graph-"
        "DB-specific optimisation."
    ),
)
async def expand(
    tenant: Tenant,
    request: Request,
    principal: ReadPrincipal,
    node_id: Annotated[
        str,
        Query(
            min_length=1,
            description="Consumer-assigned id of the seed node. Ignored when `seeds` is provided.",
        ),
    ],
    seeds: Annotated[
        list[str] | None,
        Query(
            description=(
                "Multi-seed BFS: list of seed node ids. When set, `node_id` is ignored "
                "and the BFS starts from every seed concurrently in one call. Use this "
                "when expanding from N>1 nodes -- one batched call is ~N× faster than "
                "N single-seed calls."
            ),
        ),
    ] = None,
    rel_types: Annotated[
        list[str] | None,
        Query(description="If set, traverse only edges of these types."),
    ] = None,
    max_hops: Annotated[
        int,
        Query(ge=1, le=MAX_EXPAND_HOPS, description="Path length cap (inclusive)."),
    ] = DEFAULT_EXPAND_HOPS,
    direction: Annotated[
        str,
        Query(pattern="^(in|out|both)$", description="Edge direction relative to seed."),
    ] = "both",
    limit: Annotated[
        int, Query(ge=1, le=MAX_EXPAND_LIMIT, description="Max reached nodes to return."),
    ] = DEFAULT_EXPAND_LIMIT,
    communities: Annotated[
        list[str] | None,
        Query(description=(
            "Community-id scope.  When set, the walk only reaches nodes "
            "whose ``community_id`` (from the last community recompute) is "
            "in this list, in every direction: bounded to the seed's "
            "Louvain partition(s).  Edges added since the last recompute "
            "are not followed by a scoped walk.  Empty / unset = no scope."
        )),
    ] = None,
) -> ExpandResponse:
    _check_tenant(tenant, principal)

    if rel_types is not None:
        for rt in rel_types:
            try:
                validate_label_optional(rt)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ) from exc

    # AGE Cypher variable-length expand ``MATCH (n {id:X})-[r*1..3]-(m)``
    # builds a Cartesian explosion across the inheritance children at
    # 4 M-edge scale -- 10+ s even for small max_hops, frequently
    # timing out outright.  Equivalent BFS on the per-label storage
    # tables uses the btree(start_id, end_id) we install at bootstrap
    # and stays sub-second per hop for bounded frontiers.
    started = time.perf_counter()
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        # 1. Resolve seed ext_id(s) → graphid(s).  ``seeds`` (list)
        #    takes precedence over ``node_id`` (singular) when set --
        #    one batched call expanding from N seeds concurrently is
        #    ~N× faster than N sequential single-seed calls.
        seed_ext_ids: list[str] = list(seeds) if seeds else [node_id]
        # Dedup + preserve insertion order.
        seed_ext_ids = list(dict.fromkeys(s for s in seed_ext_ids if s))
        if not seed_ext_ids:
            return ExpandResponse(
                tenant=tenant, node_id=node_id, max_hops=max_hops,
                direction=direction,
                rel_types=list(rel_types) if rel_types else None,
                limit=limit, truncated=False,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                paths=[],
            )
        # Resolve seed external ids → graphids via the same
        # ``= ANY(ARRAY(SELECT ('"' || t || '"')::agtype FROM unnest(...)))``
        # pattern /induced_edges uses.  Hits each child's
        # ``<Label>_id_unique`` btree via Index Scan — empirically
        # ~70 ms for 10 seeds vs ~855 ms (planning + execution) for
        # the previous N-OR'd ``properties @> $i::agtype`` form.  The
        # OR-tree shape forced the planner to enumerate each child x
        # each OR branch at planning time, dominating /expand's
        # fixed cost.
        seed_rows = await conn.fetch(
            f'SELECT id::text AS gid, properties::text AS props '
            f'FROM "{tenant}"."_ag_label_vertex" v '
            f"WHERE (v.properties -> '\"id\"'::agtype) = ANY ( "
            f"  ARRAY(SELECT ('\"' || t || '\"')::agtype "
            f"        FROM unnest($1::text[]) AS t) "
            f');',
            seed_ext_ids,
        )
        if not seed_rows:
            return ExpandResponse(
                tenant=tenant, node_id=node_id, max_hops=max_hops,
                direction=direction,
                rel_types=list(rel_types) if rel_types else None,
                limit=limit, truncated=False,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                paths=[],
            )
        seed_gids: list[str] = [str(r["gid"]) for r in seed_rows]
        # Seed graphids are recorded as hop=0 so their successors get
        # tagged correctly; results never include the seeds themselves.

        # 2. Discover edge-label tables (or use caller's whitelist).
        if rel_types:
            edge_labels = list(rel_types)
        else:
            label_rows = await conn.fetch(
                "SELECT c.relname AS label FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_inherits i ON i.inhrelid = c.oid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE n.nspname = $1 AND c.relkind = 'r' "
                "AND p.relname = '_ag_label_edge' ORDER BY c.relname;",
                tenant,
            )
            edge_labels = [r["label"] for r in label_rows]
        # Sanitise rel-type identifiers; bad ones silently dropped.
        safe_rels: list[str] = []
        for r in edge_labels:
            try:
                validate_label_optional(r)
                safe_rels.append(r)
            except ValueError:
                continue

        # 3. BFS: ``first_hit[gid] = (hops, rel_path)``.  Visit each
        #    node only once -- first arrival wins (shortest path).
        #    Multi-seed: every seed enters the visited set at hop=0,
        #    and the initial frontier contains all of them.  Results
        #    never include the seeds themselves (caller already has
        #    them; they'd just be noise in the BFS output).
        first_hit: dict[str, tuple[int, list[str]]] = {
            g: (0, []) for g in seed_gids
        }
        frontier: list[tuple[str, list[str]]] = [(g, []) for g in seed_gids]
        results_gids: list[str] = []
        out_dir = direction in ("out", "both")
        in_dir = direction in ("in", "both")

        # rel-type whitelist for the parent-inheritance query.  We
        # apply it post-JOIN via ``c.relname IN (...)`` rather than
        # one query per rel_type -- collapses N queries into 1 per
        # direction per hop.  Field-observed: a 20-rel-type expand
        # over 40 seeds was firing 1600+ small SQL queries per hop;
        # the same expansion via the parent table fires 1-2 queries
        # per hop and finishes in <1 s.
        # ``IN (NULL)`` matches nothing: an empty rel list must not render ``IN ()``.
        rel_in = ", ".join(f"'{r}'" for r in safe_rels) or "NULL"

        # Community-scope filter: a column predicate on the community of
        # the node a hop reaches, denormalised onto every edge row by the
        # Louvain writeback (see :func:`graphos.writes.
        # set_edge_end_communities`): ``end_community_id`` when walking an
        # edge forwards, ``start_community_id`` when walking it backwards.
        # The per-label partial btrees built at bootstrap
        # (``<label>_end_comm_id`` / ``<label>_start_comm_id``) combine via
        # BitmapAnd with the endpoint indexes, so the per-hop fetch stays
        # index-driven without any vertex-side join, temp-table, or
        # array-param hash build.
        #
        # Trade-off: edges added between Louvain reruns have
        # ``end_community_id IS NULL`` and are invisible to scoped
        # queries until the next refresh — acceptable freshness
        # window for readers (consumers typically re-run Louvain
        # after each bulk load).
        # Unscoped callers see new edges immediately because they
        # don't pass ``communities``.
        scope_active = bool(communities)
        out_scope = " AND e.end_community_id = ANY ($2::text[])" if scope_active else ""
        in_scope = " AND e.start_community_id = ANY ($2::text[])" if scope_active else ""
        for hop in range(1, max_hops + 1):
            if not frontier or len(results_gids) >= limit:
                break
            current_gids = [g for g, _ in frontier]
            id_csv = ", ".join(f"'{g}'::graphid" for g in current_gids)
            next_frontier: list[tuple[str, list[str]]] = []
            edge_rows: list[Any] = []
            # Per-direction edge fan-out via the AGE parent edge
            # inheritance table.  Postgres routes the scan to every
            # child label that matches the IN clause, each using its
            # own ``btree(start_id, end_id)`` index.
            #
            # When ``direction=both`` we fire OUT and IN concurrently
            # via ``pool.fetch`` (each call acquires + releases its
            # own connection from the pool — different connections so
            # there's no asyncpg single-in-flight-per-conn constraint).
            # The single-direction case stays on the outer ``conn`` to
            # avoid pool churn.  Empirically this halves the per-hop
            # wall-clock on ``direction=both`` traversals — a 3-hop
            # 40-seed expand was 1+ s of sequential round-trips; the
            # gather collapses 6 serial fetches to 3 parallel pairs.
            out_limit = max(limit * 4, 1000)
            out_sql = (
                f'SELECT c.relname AS rel, '
                f'       e.start_id::text AS from_gid, '
                f'       e.end_id::text   AS to_gid '
                f'FROM "{tenant}"."_ag_label_edge" e '
                f'JOIN pg_class c ON c.oid = e.tableoid '
                f'WHERE e.start_id IN ({id_csv}) '
                f'  AND c.relname IN ({rel_in}) '
                f'{out_scope} '
                f'ORDER BY e.start_id, e.end_id, c.relname '
                f'LIMIT $1;'
            ) if out_dir else None
            in_sql = (
                f'SELECT c.relname AS rel, '
                f'       e.end_id::text   AS from_gid, '
                f'       e.start_id::text AS to_gid '
                f'FROM "{tenant}"."_ag_label_edge" e '
                f'JOIN pg_class c ON c.oid = e.tableoid '
                f'WHERE e.end_id IN ({id_csv}) '
                f'  AND c.relname IN ({rel_in}) '
                f'{in_scope} '
                f'ORDER BY e.end_id, e.start_id, c.relname '
                f'LIMIT $1;'
            ) if in_dir else None
            # When scope is active, $2 = community_id strings as text[],
            # served by the per-edge-label partial btrees directly.
            args = (out_limit, list(communities)) if scope_active else (out_limit,)
            try:
                if out_sql and in_sql:
                    out_rows, in_rows = await asyncio.gather(
                        pool.fetch(out_sql, *args),
                        pool.fetch(in_sql, *args),
                    )
                    edge_rows.extend(out_rows)
                    edge_rows.extend(in_rows)
                elif out_sql:
                    edge_rows.extend(await conn.fetch(out_sql, *args))
                elif in_sql:
                    edge_rows.extend(await conn.fetch(in_sql, *args))
            except asyncpg.exceptions.QueryCanceledError:
                raise
            except asyncpg.exceptions.PostgresError as exc:
                raise _read_failed(f"expand (hop {hop})", exc) from exc

            # Map current-frontier gid -> its rel_path so we can
            # extend it for next-hop nodes.
            path_by_gid = {g: rp for g, rp in frontier}
            for r in edge_rows:
                if len(results_gids) >= limit:
                    break
                from_gid = str(r["from_gid"])
                to_gid = str(r["to_gid"])
                if to_gid in first_hit:
                    continue  # already seen at a shorter hop
                parent_path = path_by_gid.get(from_gid, [])
                new_path = [*parent_path, str(r["rel"])]
                first_hit[to_gid] = (hop, new_path)
                next_frontier.append((to_gid, new_path))
                results_gids.append(to_gid)
            frontier = next_frontier

        # 4. Bulk-resolve endpoint label + properties via tableoid JOIN.
        gid_to_meta: dict[str, tuple[str, dict[str, Any]]] = {}
        if results_gids:
            id_csv = ", ".join(f"'{g}'::graphid" for g in results_gids)
            vrows = await conn.fetch(
                f'SELECT v.id::text AS gid, '
                f'       v.properties::text AS props, '
                f'       c.relname AS lbl '
                f'FROM "{tenant}"."_ag_label_vertex" v '
                f'JOIN pg_class c ON c.oid = v.tableoid '
                f'WHERE v.id IN ({id_csv});'
            )
            for vr in vrows:
                gid_to_meta[str(vr["gid"])] = (
                    str(vr["lbl"]),
                    _safe_decode_props(vr["props"]),
                )

    # 5. Build PathHit list ordered by hops ASC, then by ext_id.
    paths: list[PathHit] = []
    for gid in results_gids:
        hops, rel_path = first_hit[gid]
        lbl, props = gid_to_meta.get(gid, ("", {}))
        ext_id = str(props.get("id") or gid)
        paths.append(PathHit(
            end_id=ext_id,
            end_label=lbl,
            end_properties=props,
            hops=hops,
            rel_path=rel_path,
        ))
    paths.sort(key=lambda p: (p.hops, p.end_id))

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return ExpandResponse(
        tenant=tenant,
        node_id=node_id,
        max_hops=max_hops,
        direction=direction,
        rel_types=list(rel_types) if rel_types else None,
        limit=limit,
        truncated=len(paths) == limit,
        elapsed_ms=elapsed_ms,
        paths=paths,
    )


# ── Edges aggregate ─────────────────────────────────────────────────────────


@router.post(
    "/edges/aggregate",
    response_model=EdgesAggregateResponse,
    summary="Group edges by endpoint properties and count",
    description=(
        "Groups edges by `(src.<group_by_src>, dst.<group_by_dst>, type(r))` "
        "and returns a per-group count. Optional filters on edge type, "
        "values of the grouping property on each side, and a "
        "`require_different` flag for the cross-grouping case "
        "(e.g. edges between teams: `group_by_src=team, "
        "group_by_dst=team, require_different=true`)."
    ),
)
async def aggregate_edges(
    tenant: Tenant,
    body: EdgesAggregateRequest,
    request: Request,
    principal: ReadPrincipal,
) -> EdgesAggregateResponse:
    _check_tenant(tenant, principal)

    # group_by_* keys are already validated by the Pydantic body; the regex
    # rejects anything that wouldn't be safe to interpolate into Cypher.
    src_key = body.group_by_src
    dst_key = body.group_by_dst

    if body.rel_types is not None:
        for rt in body.rel_types:
            try:
                validate_label_optional(rt)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ) from exc

    # AGE Cypher MATCH (a)-[r]->(b) GROUP BY a.<src>, b.<dst>, type(r)
    # times out at 4 M-edge scale -- the planner scans every edge label
    # table and probes every inheritance-descended vertex via the agtype
    # property bag.  Equivalent SQL on the per-label edge storage
    # tables joined against the parent vertex inheritance table uses
    # the GIN(properties) we install per child label, plus the
    # btree(start_id, end_id) edge index -- bounded latency at any
    # graph size.
    started = time.perf_counter()
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        if body.rel_types:
            edge_labels = list(body.rel_types)
        else:
            label_rows = await conn.fetch(
                "SELECT c.relname AS label FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_inherits i ON i.inhrelid = c.oid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE n.nspname = $1 AND c.relkind = 'r' "
                "AND p.relname = '_ag_label_edge' ORDER BY c.relname;",
                tenant,
            )
            edge_labels = [r["label"] for r in label_rows]
    safe_rels: list[str] = []
    for rt in edge_labels:
        try:
            validate_label_optional(rt)
            safe_rels.append(rt)
        except ValueError:
            continue
    all_groups: list[tuple[str, str, str, int]] = []
    truncated = False
    if safe_rels:
        all_groups, truncated = await _aggregate_edge_groups(
            pool, tenant, body, sorted(set(safe_rels)),
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    groups = [
        AggregateRow(
            src_value=src_val,
            dst_value=dst_val,
            rel_type=rel,
            count=cnt,
        )
        for src_val, dst_val, rel, cnt in all_groups
    ]

    return EdgesAggregateResponse(
        tenant=tenant,
        group_by_src=src_key,
        group_by_dst=dst_key,
        rel_types=list(body.rel_types) if body.rel_types else None,
        truncated=truncated,
        elapsed_ms=elapsed_ms,
        groups=groups,
    )


def _agtype_text_value(raw: str | None) -> str:
    """An agtype rendered as text, as the endpoint has always returned it:
    no ``::agtype`` suffix, strings without their quotes."""
    val = raw or ""
    if val.endswith("::agtype"):
        val = val.rsplit("::", 1)[0]
    if len(val) >= 2 and val.startswith('"') and val.endswith('"'):  # noqa: PLR2004
        val = val[1:-1]
    return val


async def _aggregate_edge_groups(
    pool: asyncpg.Pool,
    tenant: str,
    body: EdgesAggregateRequest,
    rels: list[str],
) -> tuple[list[tuple[str, str, str, int]], bool]:
    """The top ``body.limit`` ``(src_value, dst_value, rel_type, count)``
    groups, and whether there were more.

    One statement.  The grouping property of each side is first extracted
    into a narrow ``(graphid, value)`` map of the vertices that have it;
    the edges of the requested types are then joined to those maps and
    grouped.  Joining edges to whole vertex rows instead (properties and
    all) was what made each per-type query's hash join run out of shared
    memory on large graphs.

    Ordering is count descending, then source value, destination value and
    edge type, compared as the returned strings.  The query returns every
    group tied with the last one kept, and the sort and cut happen here, so
    the same groups come back however ties fall in SQL.
    """
    def side(key: str, param: int) -> str:
        value = f"""(v.properties -> '"{key}"'::agtype)"""
        return f"""
            SELECT v.id, {value} AS val
            FROM "{tenant}"."_ag_label_vertex" v
            WHERE {value} IS NOT NULL
              AND (${param}::text[] IS NULL
                   OR {value} IN (SELECT x::agtype FROM unnest(${param}::text[]) AS x))"""

    edges = " UNION ALL ".join(
        f"SELECT start_id, end_id, '{rel}'::text AS rel FROM \"{tenant}\".\"{rel}\""
        for rel in rels
    )
    different = "WHERE s.val <> d.val" if body.require_different else ""
    sql = f"""
        WITH src AS MATERIALIZED ({side(body.group_by_src, 2)}),
             dst AS MATERIALIZED ({side(body.group_by_dst, 3)}),
             groups AS MATERIALIZED (
                SELECT s.val::text AS src_value, d.val::text AS dst_value,
                       e.rel AS rel_type, count(*)::bigint AS n
                FROM ({edges}) e
                JOIN src s ON s.id = e.start_id
                JOIN dst d ON d.id = e.end_id
                {different}
                GROUP BY s.val, d.val, e.rel
             ),
             cut AS (SELECT n FROM groups ORDER BY n DESC OFFSET $1 LIMIT 1)
        SELECT src_value, dst_value, rel_type, n, (SELECT n FROM cut) AS cut_n
        FROM groups
        WHERE n >= COALESCE((SELECT n FROM cut), 0);
    """

    def values(vals: list[str] | None) -> list[str] | None:
        return [_json.dumps(v) for v in vals] if vals else None

    async with pool.acquire() as conn, conn.transaction():
        # Parallel workers each take their own shared-memory hash tables;
        # one backend keeps the footprint to one query's worth.
        await conn.execute("SET LOCAL max_parallel_workers_per_gather = 0;")
        # The vertex maps are materialized CTEs, which carry no statistics:
        # the planner then overestimates each join by orders of magnitude
        # and picks nested loops over every vertex pair (hours on a large
        # graph).  This query is always one scan of the edges plus two
        # small hash tables; say so.
        await conn.execute("SET LOCAL enable_nestloop = off;")
        await conn.execute("SET LOCAL enable_mergejoin = off;")
        try:
            rows = await conn.fetch(
                sql, body.limit, values(body.src_values), values(body.dst_values),
            )
        except asyncpg.exceptions.QueryCanceledError:
            raise
        except asyncpg.exceptions.PostgresError as exc:
            _aggregate_log.warning("aggregate_edges failed: %s", str(exc)[:200])
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"aggregate failed: {exc}",
            ) from exc

    found = [
        (_agtype_text_value(r["src_value"]), _agtype_text_value(r["dst_value"]),
         str(r["rel_type"]), int(r["n"]))
        for r in rows
    ]
    found.sort(key=lambda g: (-g[3], g[0], g[1], g[2]))
    truncated = bool(rows) and rows[0]["cut_n"] is not None
    return found[: body.limit], truncated


@router.post(
    "/nodes/aggregate",
    response_model=NodesAggregateResponse,
    summary="Group vertices by a property and count",
    description=(
        "Groups vertices of `label` by `n.<group_by>` and returns "
        "`{value, count}` rows ordered by descending count. Inheritance-"
        "aware: passing a parent label (e.g. `Document`) matches all "
        "child labels via postgres table inheritance — the same "
        "semantic as Cypher `MATCH (n:Document)`. Useful for live "
        "histograms (e.g. count per `language` or per `type`) "
        "without precomputing a summary. `missing` also counts the nodes "
        "without the property, under `\"\"`; `total` is every node counted "
        "and `distinct` every group, `limit` aside. A string value is "
        "returned as it is, any other value as JSON."
    ),
)
async def aggregate_nodes(
    tenant: Tenant,
    body: NodesAggregateRequest,
    request: Request,
    principal: ReadPrincipal,
) -> NodesAggregateResponse:
    _check_tenant(tenant, principal)

    # label + group_by are regex-validated by the Pydantic body so the
    # interpolation below is safe.  Reading the label's table without ONLY
    # includes the tables of labels inheriting from it, as Cypher
    # ``MATCH (n:<label>)`` did.  Grouping is on the agtype value, so
    # values compare exactly as they did in Cypher (``values`` match
    # string properties only).
    label = body.label
    value = f"""(n.properties -> '"{body.group_by}"'::agtype)"""
    sql = f"""
        SELECT {value}::text AS v, count(*) AS c, sum(count(*)) OVER ()::bigint AS total,
               count(*) OVER () AS n_groups
        FROM "{tenant}"."{label}" n
        WHERE CASE WHEN {value} IS NULL THEN $3
                   ELSE $2::text[] IS NULL
                        OR {value} IN (SELECT x::agtype FROM unnest($2::text[]) AS x)
              END
        GROUP BY {value}
        ORDER BY count(*) DESC, {value}
        LIMIT $1
    """
    wanted = [_json.dumps(v) for v in body.values] if body.values else None

    started = time.perf_counter()
    try:
        async with request.app.state.pool.acquire() as conn:
            rows = await conn.fetch(sql, body.limit, wanted, body.missing)
    except asyncpg.exceptions.UndefinedTableError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"label '{label}' not registered for tenant '{tenant}'",
        ) from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    groups = [
        NodeAggregateRow(value=_group_value(row["v"]), count=int(row["c"]))
        for row in rows
    ]
    return NodesAggregateResponse(
        tenant=tenant,
        label=label,
        group_by=body.group_by,
        truncated=len(groups) == body.limit,
        elapsed_ms=elapsed_ms,
        total=int(rows[0]["total"]) if rows else 0,
        distinct=int(rows[0]["n_groups"]) if rows else 0,
        groups=groups,
    )


def _group_value(text: str | None) -> str:
    """A group's value: ``""`` when missing, a string as it is, anything else as JSON."""
    if text is None:
        return ""
    value = parse_agtype(text)
    return value if isinstance(value, str) else _json.dumps(value, ensure_ascii=False)


@router.post(
    "/sample_subgraph",
    response_model=SampleSubgraphResponse,
    summary="Stratified edge-and-node sample for graph visualisation",
    description=(
        "Returns up to `per_rel` edges per relationship type, with both "
        "endpoints expanded to the property bag selected by "
        "`include_properties`.  Caller-supplied `label` / "
        "`filter_property` / `filter_value` keep the endpoint tenant-"
        "agnostic — graphos doesn't know about any consumer's scoping "
        "property or node label; the caller specifies them.  When `rel_types` is "
        "omitted, the endpoint discovers them via the edge histogram "
        "first.  Intended for feeding a graph visualisation."
    ),
)
async def sample_subgraph(
    tenant: Tenant,
    body: SampleSubgraphRequest,
    request: Request,
    principal: ReadPrincipal,
) -> SampleSubgraphResponse:
    _check_tenant(tenant, principal)

    if body.filter_property is not None and body.filter_value is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="filter_value is required when filter_property is set",
        )

    label = body.label
    filter_prop = body.filter_property
    filter_val = body.filter_value
    per_rel = int(body.per_rel)

    # Property-bag projection.  When include_properties is empty we
    # return only ``id`` -- minimal payload for callers that just want
    # the topology.  Validate each requested property name with the
    # same regex the body uses on label/group_by; safe to interpolate.
    safe_props: list[str] = []
    for p in body.include_properties:
        try:
            safe_props.append(_validate_prop_key(p))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"include_properties: {exc}",
            ) from exc
    safe_edge_props: list[str] = []
    for p in body.edge_properties:
        try:
            safe_edge_props.append(_validate_prop_key(p))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"edge_properties: {exc}",
            ) from exc

    # Optional scoping filter is applied as a Python post-fetch
    # comparison further down (see the ``filter_prop`` block in the
    # row loop).
    started = time.perf_counter()
    pool = request.app.state.pool

    # AGE Cypher path is unusably slow at high-edge-count scale: a
    # plain ``MATCH (n:Document)-[r:CITES]->(m:Document) LIMIT 5`` can
    # take 70+ s on a 4 M-edge graph because the planner joins across
    # every child table descended from the parent label.  Equivalent raw SQL on
    # the per-label storage tables completes in ~3 s.  This endpoint
    # therefore goes straight to the AGE storage tables: each edge-
    # label table is a regular Postgres relation
    # ``"<tenant>"."<rel_type>" (id graphid, start_id graphid,
    # end_id graphid, properties agtype)`` and the parent vertex
    # inheritance table ``"<tenant>"._ag_label_vertex`` covers all
    # child label tables in a single scan.
    #
    # The ``label`` body argument constrains which vertex labels we
    # accept endpoints from -- we leverage AGE's per-label-id encoding
    # in graphid (upper bits) by JOINing against the parent vertex
    # table whose row visibility includes only descendants of the
    # caller-supplied label.  When ``label`` is the umbrella parent
    # (e.g. Document), inheritance includes every leaf label by default.

    # ── 1. Discover rel-types if caller didn't whitelist ─────────────
    if body.rel_types:
        for rt in body.rel_types:
            try:
                validate_label_optional(rt)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ) from exc
        rel_types = list(body.rel_types)
    else:
        # Discover edge labels from ag_catalog.  Cheap: one row per
        # edge label, bounded by the tenant's schema size.
        async with pool.acquire() as conn:
            row_recs = await conn.fetch(
                "SELECT c.relname AS label "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_inherits i ON i.inhrelid = c.oid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE n.nspname = $1 "
                "  AND c.relkind = 'r' "
                "  AND p.relname = '_ag_label_edge' "
                "ORDER BY c.relname;",
                tenant,
            )
        rel_types = [r["label"] for r in row_recs]

    # ── 2. Per-rel SQL sample on the edge label tables ───────────────
    nodes_by_id: dict[str, dict[str, Any]] = {}
    edges_out: list[SampleEdgeHit] = []
    all_endpoint_ids: set[str] = set()

    async with pool.acquire() as conn:
        for rel in rel_types:
            try:
                validate_label_optional(rel)
            except ValueError:
                continue
            # Optional property filter on endpoints: jsonb-extract via
            # the agtype ``->`` operator on the parent vertex table.
            # Skip on the edge SELECT (no property bag on edges to
            # filter); applied during endpoint resolution below.
            # Edge property bag (agtype) is cast to text → json so
            # asyncpg can decode into a Python dict.
            try:
                edge_rows = await conn.fetch(
                    f'SELECT start_id::text AS src_id, '
                    f'       end_id::text   AS dst_id, '
                    f'       properties::text AS props '
                    f'FROM "{tenant}"."{rel}" '
                    f'LIMIT $1;',
                    per_rel,
                )
            except asyncpg.exceptions.QueryCanceledError:
                raise
            except asyncpg.exceptions.UndefinedTableError:
                continue  # a type without a table has no edges to sample
            except asyncpg.exceptions.PostgresError as exc:
                raise _read_failed(f"sample_subgraph ({rel})", exc) from exc
            for r in edge_rows:
                src = str(r["src_id"])
                dst = str(r["dst_id"])
                props_raw = r["props"] or "{}"
                # agtype text strips the ``::agtype`` annotation in
                # most cases; tolerate it defensively.
                if props_raw.endswith("::agtype"):
                    props_raw = props_raw.rsplit("::", 1)[0]
                try:
                    e_props_full = _json.loads(props_raw)
                except (_json.JSONDecodeError, TypeError):
                    e_props_full = {}
                edge_props = {
                    p: e_props_full[p]
                    for p in safe_edge_props if p in e_props_full
                }
                edges_out.append(SampleEdgeHit(
                    src_id=src, dst_id=dst, type=rel, properties=edge_props,
                ))
                all_endpoint_ids.add(src)
                all_endpoint_ids.add(dst)

        # ── 3. Bulk-fetch endpoint properties from the parent
        #      vertex inheritance table (one query covers all child
        #      labels via postgres inheritance).  Needed for the projection
        #      and also for ``filter_property`` even when nothing is projected.
        if all_endpoint_ids and (safe_props or filter_prop is not None):
            ids_list = list(all_endpoint_ids)
            # asyncpg handles graphid as bigint internally; build the
            # IN list as text-cast-back to graphid.
            id_csv = ", ".join(f"'{i}'::graphid" for i in ids_list)
            vertex_sql = (
                f'SELECT id::text AS gid, properties::text AS props '
                f'FROM "{tenant}"."{label}" '
                f'WHERE id IN ({id_csv});'
            )
            try:
                vrows = await conn.fetch(vertex_sql)
            except asyncpg.exceptions.QueryCanceledError:
                raise
            except asyncpg.exceptions.UndefinedTableError:
                vrows = []  # a label without a table holds none of the ends
            except asyncpg.exceptions.PostgresError as exc:
                raise _read_failed("sample_subgraph (endpoints)", exc) from exc
            # Optional property filter on the endpoints.  Apply
            # post-hoc in Python -- the WHERE clause on the agtype
            # operator (``properties->'"X"' = $fv::agtype``) doesn't
            # use any index we install and would re-introduce the
            # per-row scan cost the SQL path is here to avoid.
            for vr in vrows:
                gid = str(vr["gid"])
                props_raw = vr["props"] or "{}"
                if props_raw.endswith("::agtype"):
                    props_raw = props_raw.rsplit("::", 1)[0]
                try:
                    n_props_full = _json.loads(props_raw)
                except (_json.JSONDecodeError, TypeError):
                    n_props_full = {}
                if (
                    filter_prop is not None
                    and filter_val is not None
                    and str(n_props_full.get(filter_prop, "")) != filter_val
                ):
                    continue
                n_props = {
                    p: n_props_full[p]
                    for p in safe_props if p in n_props_full
                }
                nodes_by_id[gid] = {"id": gid, "properties": n_props}

    # Drop edges whose endpoints were filtered out (when filter_prop
    # is set).  Keeps the response self-consistent.
    if filter_prop is not None:
        kept_ids = set(nodes_by_id.keys())
        edges_out = [
            e for e in edges_out
            if e.src_id in kept_ids and e.dst_id in kept_ids
        ]
    elif safe_props:
        # When no filter is applied, every endpoint should be in
        # nodes_by_id; fill any that weren't resolved as id-only.
        for gid in all_endpoint_ids:
            nodes_by_id.setdefault(gid, {"id": gid, "properties": {}})
    else:
        # No property projection requested -- return id-only nodes.
        for gid in all_endpoint_ids:
            nodes_by_id.setdefault(gid, {"id": gid, "properties": {}})

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    nodes_out = [SampleNodeHit(**v) for v in nodes_by_id.values()]
    return SampleSubgraphResponse(
        tenant=tenant,
        label=label,
        rel_types=rel_types,
        per_rel=per_rel,
        truncated=False,  # per-rel cap is the bound -- not a global truncate
        elapsed_ms=elapsed_ms,
        nodes=nodes_out,
        edges=edges_out,
    )


# ── Deletes ─────────────────────────────────────────────────────────────────


@router.delete(
    "/nodes/{external_id:path}",
    response_model=DeleteNodeResponse,
    summary="Delete a node and its edges by its consumer-assigned id",
    description=(
        "Idempotent: missing nodes return 0/0. Every node with that id (in "
        "any label, or only in ``label`` when given) is removed together with "
        "all its incident edges, in one transaction.\n\n"
        "With ``If-Match`` the id must name exactly one node (409 otherwise), "
        "deleted only if it still has that ETag (412 otherwise)."
    ),
    responses={
        409: {"description": "With `If-Match`: the id exists in several labels."},
        **_PRECONDITION_RESPONSES,
    },
)
async def delete_node(  # noqa: PLR0913 — FastAPI route parameters
    tenant: Tenant,
    external_id: ExternalId,
    request: Request,
    principal: WritePrincipal,
    label: Annotated[
        str | None, Query(description="Delete the node in this label only."),
    ] = None,
    if_match: IfMatchHeader = None,
) -> DeleteNodeResponse:
    _check_tenant(tenant, principal)
    _check_vertex_label(tenant, label)
    precondition = _if_match(request, if_match)

    pool = request.app.state.pool
    started = time.perf_counter()
    try:
        if precondition is None:
            nodes_n, edges_n = await writes.delete_nodes(
                pool, tenant=tenant, property_key="id", property_value=external_id, label=label,
            )
        else:
            nodes_n, edges_n = await writes.delete_node(
                pool, tenant=tenant, external_id=external_id, label=label, if_match=precondition,
            )
    except lookup.PreconditionFailedError as exc:
        raise _precondition_failed(exc) from exc
    except lookup.AmbiguousMatchError as exc:
        raise _node_ambiguous(external_id, exc) from exc
    except CypherError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"cypher error: {exc}",
        ) from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return DeleteNodeResponse(
        tenant=tenant,
        node_id=external_id,
        nodes_deleted=nodes_n,
        edges_deleted=edges_n,
        elapsed_ms=elapsed_ms,
    )


# A deadlock, a serialization failure.
_LOCK_CONFLICTS = frozenset({"40P01", "40001"})


@router.post(
    "/nodes:delete",
    response_model=DeleteNodesResponse,
    summary="Delete a batch of nodes and their edges by label and id",
    description=(
        "Removes each node named by `label` and `id`, together with all its "
        "incident edges, in one transaction: all of them or none. "
        f"Up to {MAX_NODES_UPSERT_BATCH} nodes per request.\n\n"
        "Idempotent: a node that does not exist is skipped, so a repeat "
        "deletes nothing. A label takes in its child labels' nodes, as the "
        "single delete does. Every label must be registered for the tenant (400 "
        "otherwise, and nothing is deleted)."
    ),
    responses={503: {
        "description": "Lost to concurrent writes on the same edges; nothing was deleted. Retry.",
    }},
)
async def delete_nodes(
    tenant: Tenant,
    body: NodesDeleteRequest,
    request: Request,
    principal: WritePrincipal,
) -> DeleteNodesResponse:
    _check_tenant(tenant, principal)
    for label in sorted({n.label for n in body.nodes}):
        _check_vertex_label(tenant, label)

    pool = request.app.state.pool
    started = time.perf_counter()
    try:
        nodes_n, edges_n = await writes.delete_nodes_by_ref(
            pool, tenant=tenant, refs=[(n.label, n.id) for n in body.nodes],
        )
    except CypherError as exc:
        if exc.sqlstate in _LOCK_CONFLICTS:
            # Lost to another delete every time: nothing was deleted, and a retry is safe.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="another write held these nodes' edges; nothing was deleted, try again",
                headers={"Retry-After": "1"},
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"cypher error: {exc}",
        ) from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return DeleteNodesResponse(
        tenant=tenant,
        nodes_requested=len(body.nodes),
        nodes_deleted=nodes_n,
        edges_deleted=edges_n,
        elapsed_ms=elapsed_ms,
    )


@router.delete(
    "/nodes",
    response_model=DeleteByPropertyResponse,
    summary="Tombstone every node (and its edges) matching a single-property predicate",
    description=(
        "Removes every node whose `<property_key>` equals the string "
        "`<property_value>`, along with all incident edges, in one "
        "transaction. Used for scope-wide tombstones (e.g. "
        "`property_key=project_id`). The property key must match the "
        "standard identifier regex."
    ),
)
async def delete_by_property(
    tenant: Tenant,
    request: Request,
    principal: WritePrincipal,
    property_key: Annotated[
        str,
        Query(min_length=1, max_length=63, description="Property name (e.g. project_id)."),
    ],
    property_value: Annotated[
        str,
        Query(min_length=1, description="Property value to match."),
    ],
) -> DeleteByPropertyResponse:
    _check_tenant(tenant, principal)

    # Property keys are interpolated into SQL — whitelist-validate to the
    # same regex used by the aggregate endpoint.
    try:
        _validate_prop_key(property_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    pool = request.app.state.pool
    started = time.perf_counter()
    try:
        nodes_n, edges_n = await writes.delete_nodes(
            pool, tenant=tenant, property_key=property_key,
            property_value=property_value,
        )
    except CypherError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"cypher error: {exc}",
        ) from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return DeleteByPropertyResponse(
        tenant=tenant,
        property_key=property_key,
        property_value=property_value,
        nodes_deleted=nodes_n,
        edges_deleted=edges_n,
        elapsed_ms=elapsed_ms,
    )


# ── Export (graphology JSON for sigma.js) ──────────────────────────────────


@router.get(
    "/export",
    response_model=GraphologyExport,
    summary="Export a subgraph in graphology's native serialization format",
    description=(
        "Output shape is graphology's native serialization (see "
        "https://graphology.github.io/serialization.html). sigma.js "
        "consumers call ``Graph.from(payload)`` directly — no extra "
        "JS dependency, no client-side parsing. The same shape is also "
        "consumable by other graphology-aware tooling.\n\n"
        "By default exports up to `limit_nodes` nodes (capped) and every "
        "edge between them. Optional filters: `node_ids` selects a "
        "specific seed set; `rel_types` restricts the edges to those types."
    ),
)
async def export_graph(
    tenant: Tenant,
    request: Request,
    principal: ReadPrincipal,
    node_ids: Annotated[
        list[str] | None,
        Query(description="If set, export only these nodes + edges among them."),
    ] = None,
    rel_types: Annotated[
        list[str] | None,
        Query(description="Restrict edges to these types."),
    ] = None,
    limit_nodes: Annotated[
        int, Query(ge=1, le=MAX_EXPORT_NODE_LIMIT, description="Max nodes to export."),
    ] = DEFAULT_EXPORT_NODE_LIMIT,
) -> GraphologyExport:
    _check_tenant(tenant, principal)
    # It hands out a subgraph of up to 50,000 nodes at once: audited as a read.
    audit_this(request)

    if rel_types is not None:
        for rt in rel_types:
            try:
                validate_label_optional(rt)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ) from exc

    pool = request.app.state.pool
    # Step 1: the nodes.  By id through each label's unique index, or any
    # ``limit_nodes`` nodes.
    by_id = (
        "WHERE (v.properties -> '\"id\"') = ANY(ARRAY("
        "  SELECT x::agtype FROM unnest($2::text[]) AS x))"
        if node_ids else
        "WHERE (v.properties -> '\"id\"') IS NOT NULL"
    )
    node_sql = f"""
        SELECT v.id::text AS gid, c.relname AS lbl, v.properties::text AS props
        FROM "{tenant}"."_ag_label_vertex" v
        JOIN pg_class c ON c.oid = v.tableoid
        {by_id}
        LIMIT $1
    """
    node_args: list[object] = [limit_nodes]
    if node_ids:
        node_args.append([_json.dumps(i) for i in node_ids])
    async with pool.acquire() as conn:
        found = await conn.fetch(node_sql, *node_args)
    if not found:
        return GraphologyExport()

    ext_of_gid: dict[str, str] = {}
    node_rows = []
    for r in found:
        props = _safe_decode_props(r["props"])
        ext = str(props.get("id"))
        ext_of_gid[r["gid"]] = ext
        node_rows.append({"id": ext, "lbl": r["lbl"], "props": props})

    # Step 2: edges whose two endpoints are both among those nodes,
    # through the (start_id, end_id) index of each edge table.
    rel_filter = "AND c.relname = ANY($2::text[])" if rel_types else ""
    edge_sql = f"""
        WITH ids AS MATERIALIZED (
            SELECT agtype_to_graphid(x::agtype) AS gid FROM unnest($1::text[]) AS x
        )
        SELECT e.id::text AS rid, e.start_id::text AS src, e.end_id::text AS dst,
               c.relname AS rel_type, e.properties::text AS rprops
        FROM "{tenant}"."_ag_label_edge" e
        JOIN pg_class c ON c.oid = e.tableoid
        WHERE e.start_id = ANY(ARRAY(SELECT gid FROM ids))
          AND e.end_id = ANY(ARRAY(SELECT gid FROM ids))
          {rel_filter}
    """
    edge_args: list[object] = [list(ext_of_gid)]
    if rel_types:
        edge_args.append(list(rel_types))
    async with pool.acquire() as conn:
        found_edges = await conn.fetch(edge_sql, *edge_args)
    edge_rows = [
        {
            "rid": r["rid"],
            "src": ext_of_gid[r["src"]],
            "dst": ext_of_gid[r["dst"]],
            "rel_type": r["rel_type"],
            "rprops": _safe_decode_props(r["rprops"]),
        }
        for r in found_edges
    ]

    # Step 3: project into graphology's native format.
    nodes = [
        GraphologyNode(
            key=str(r["id"]),
            attributes={
                **(dict(r["props"] or {})),
                # `label` is sigma.js's display field by convention; we
                # populate it from `name` if available, else fall back to
                # the AGE label name. The vertex label is also kept under
                # `_kind` so consumers that care about typing can read it.
                "label": dict(r["props"] or {}).get("name") or str(r["id"]),
                "_kind": str(r["lbl"]),
            },
        )
        for r in node_rows
    ]
    edges = [
        GraphologyEdge(
            key=f"e_{r['rid']}",
            source=str(r["src"]),
            target=str(r["dst"]),
            attributes={
                **(dict(r["rprops"] or {})),
                "label": str(r["rel_type"]),       # sigma convention
                "_type": str(r["rel_type"]),
            },
        )
        for r in edge_rows
    ]

    return GraphologyExport(nodes=nodes, edges=edges)


# ═══════════════════════════════════════════════════════════════════════════
#  POST /nodes:scan — bulk enumeration with optional filter + projection
# ═══════════════════════════════════════════════════════════════════════════
#
# Schema-agnostic enumeration primitive.  Pre-existing reads cover narrow
# questions (search-by-name, top-N-per-label, neighbors, expand) but no
# endpoint enumerates "every node matching these labels + filters" with
# property projection and cursor pagination.  /nodes:scan fills that gap
# for batch consumers (external edge resolvers, export / dump tools,
# operator scripts).
#
# Cursor encoding: base64-encoded JSON ``{"label", "order_by", "values",
# "gid"}`` — the requested label being scanned, the sort spec, the last
# row's sort-key values and its graphid.  Opaque to clients — they
# round-trip the string unchanged — but inspectable for ops when
# debugging a stuck scan.
#
# Label hierarchies: a label's table is the parent of its child labels'
# tables (``label_hierarchy``, Postgres inheritance), so a scan of the
# parent reads the children's rows too, as every read on a parent does.
# Each item reports the label of the table its row lives in (the leaf
# label), never the requested one.  Rows from several tables can tie on
# the sort keys (a unique ``id`` index is per table), so the graphid,
# unique across the graph, breaks ties: the order is total and a cursor
# resumes exactly after the last row it returned.
#
# SQL shape: AGE stores ``properties`` as ``agtype`` (binary-tagged JSON),
# not native ``jsonb`` — its ``->``/``->>`` operators take agtype-encoded
# string arguments (e.g. ``->'"id"'``) and the planner can't apply jsonb
# operators directly.  We hoist the rows through a CTE that casts
# ``properties::text::jsonb`` once per row; from there all predicates and
# the projection use standard jsonb operators.  The text-form of agtype
# is JSON, so the round-trip is lossless.


# The graphid sort key: ``order_by="_node"``, and the tiebreaker appended
# after any other sort keys.
_NODE_SORT_KEY: dict[str, Any] = {
    "order_expr": "v.id",
    "anchor_select": "v.id::text",
    "not_null": "v.id IS NOT NULL",
    "cursor_cast": "::text::agtype",
    "cursor_fn": "agtype_to_graphid",
    "desc": False,
    "is_node": True,
    "property": "_node",
}


def _normalize_order_by(
    order_by: Any,
) -> tuple[Any, list[dict[str, Any]]]:
    """Normalise the request's ``order_by`` into (wire_form, internal_keys).

    Returns:
      wire_form: the exact value used for cursor encoding / drift checks
        — a string for the single-key forms (``"id"`` / ``"_node"`` /
        ``"<prop>"``), or a list of ``{property, desc}`` dicts for the
        multi-key form.  This is what the cursor carries verbatim and
        what we compare across pages.
      internal_keys: an ordered list of per-key dicts the route uses
        to build SQL.  Each carries ``order_expr`` (SQL fragment for
        ORDER BY + cursor comparison), ``anchor_select`` (text-cast
        for the response row), ``not_null`` (predicate), ``cursor_cast``
        (param-side cast e.g. ``::text::agtype``), ``cursor_fn``
        (optional wrapper like ``agtype_to_graphid``), ``desc`` (bool),
        and ``is_node`` (bool for the graphid sentinel).
    """
    if isinstance(order_by, str):
        wire = order_by
        if order_by == "_node":
            return wire, [dict(_NODE_SORT_KEY)]
        expr = f"(v.properties -> '\"{order_by}\"'::agtype)"
        return wire, [{
            "order_expr": expr,
            "anchor_select": f"({expr})::text",
            "not_null": f"{expr} IS NOT NULL",
            "cursor_cast": "::text::agtype",
            "cursor_fn": None,
            "desc": False,
            "is_node": False,
            "property": order_by,
        }]
    # list form — sequence of NodesScanOrderClause (pydantic) or dicts
    # (after Pydantic v2 model_dump on round-trip).
    keys: list[dict[str, Any]] = []
    wire_list: list[dict[str, Any]] = []
    for clause in order_by:
        prop = (
            clause.property if hasattr(clause, "property")
            else clause["property"]
        )
        desc = bool(
            getattr(clause, "desc", None)
            if hasattr(clause, "desc")
            else clause.get("desc", False)
        )
        expr = f"(v.properties -> '\"{prop}\"'::agtype)"
        keys.append({
            "order_expr": expr,
            "anchor_select": f"({expr})::text",
            "not_null": f"{expr} IS NOT NULL",
            "cursor_cast": "::text::agtype",
            "cursor_fn": None,
            "desc": desc,
            "is_node": False,
            "property": prop,
        })
        wire_list.append({"property": prop, "desc": desc})
    return wire_list, keys


def _encode_scan_cursor(
    label: str, order_by_wire: Any, anchor_values: list[Any],
    gid: int | None = None,
) -> str:
    """Build an opaque-but-debuggable cursor token.

    Carries the order-by spec verbatim so a request that changes the
    sort keys (or their directions) mid-scan can be rejected cleanly:
    you can't compare page-N's name-sorted position to page-N+1's
    date-sorted intent.  ``anchor_values`` is a list parallel to the
    sort keys (one anchor value per key); single-key cursors carry a
    one-element list.  ``gid`` is the last row's graphid, the tiebreaker
    between rows equal on every sort key (``None`` when the sort is
    already by graphid).
    """
    obj: dict[str, Any] = {
        "label": label,
        "order_by": order_by_wire,
        "values": anchor_values,
    }
    if gid is not None:
        obj["gid"] = gid
    payload = _json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_scan_cursor(
    token: str, allowed_labels: list[str], order_by_wire: Any,
) -> tuple[str, list[Any], int | None]:
    """Decode + validate a cursor.  Returns ``(label, anchor_values, gid)``.

    ``gid`` is ``None`` for a cursor issued before the graphid tiebreaker
    existed; such a cursor resumes strictly after its sort-key values.

    Rejects cursors whose ``label`` is outside the request's labels,
    or whose ``order_by`` shape (string OR list-of-clauses) differs
    from the current request's.  Both are 400s.  Anchor values keep
    their JSON types (str / int / float / bool) so SQL comparisons
    stay type-faithful.
    """
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        obj = _json.loads(raw.decode("utf-8"))
        label = obj["label"]
        cursor_order_by = obj["order_by"]
        anchor_values = obj["values"]
        gid = obj.get("gid")
        if gid is not None:
            gid = int(gid)
    except Exception as exc:  # noqa: BLE001 — any decode failure is a 400
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid cursor: {exc.__class__.__name__}",
        ) from exc
    if not isinstance(label, str) or not isinstance(anchor_values, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cursor missing required fields 'label' / 'values'",
        )
    if label not in allowed_labels:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"cursor label '{label}' is not in the request's labels "
                f"{allowed_labels} — pass the cursor back unchanged with "
                "the same labels list to continue the scan"
            ),
        )
    # Drift check: serialise both sides and compare byte-equal.  Works
    # for both string-form and list-form because both are JSON-friendly.
    if _json.dumps(cursor_order_by, sort_keys=True) != _json.dumps(
        order_by_wire, sort_keys=True,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "cursor was issued with a different order_by than the "
                "current request — sort keys + directions must match "
                "exactly across pages of one scan"
            ),
        )
    return label, anchor_values, gid


def _cursor_branches(
    keys: list[dict[str, Any]],
    anchor_values: list[Any],
    *,
    next_param_idx: int,
) -> list[tuple[str, list[Any], int]]:
    """The rows strictly AFTER the cursor, as predicates to query in turn.

    Lexicographic "after" over N keys (each ascending or descending) is the
    disjoint union of N branches:

        k1 = v1 AND … AND k(N-1) = v(N-1) AND kN cmpN vN
        …
        k1 = v1 AND k2 cmp2 v2
        k1 cmp1 v1

    where ``cmpᵢ`` is ``>`` ascending or ``<`` descending.  Listed in this
    order, every row of a branch sorts before every row of the next, so
    querying them one after another (each ``ORDER BY`` the keys, ``LIMIT``
    what the page still needs) returns the rows in order, and each query is
    an equality lookup plus one strict range: an index scan that starts at
    the cursor.

    One ``OR`` of the branches would be the same rows but no index range,
    and a ``k1 >= v1`` bound to restore one is wrong on agtype: Apache AGE
    1.6 registers ``>=`` and ``>`` under each other's btree strategy
    numbers, so an index scan for ``>=`` starts after the rows equal to
    ``v1`` and loses them.  ``>``, ``<``, ``<=`` and ``=`` are unaffected.

    Each branch is ``(fragment, params, next_param_idx)``, its parameters
    numbered from ``next_param_idx``.
    """
    if len(keys) != len(anchor_values):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"cursor values length {len(anchor_values)} does not "
                f"match order_by length {len(keys)} — cursor is from a "
                "different request shape"
            ),
        )
    branches: list[tuple[str, list[Any], int]] = []
    for depth in range(len(keys), 0, -1):
        params: list[Any] = []
        idx = next_param_idx
        terms: list[str] = []
        for i, (k, value) in enumerate(zip(keys[:depth], anchor_values[:depth], strict=True)):
            # The cursor value gets the same cast/wrap as the expression it
            # is compared against: graphid through ``agtype_to_graphid``,
            # properties as agtype.
            if k["cursor_fn"] is not None:
                rhs = f"{k['cursor_fn']}(${idx}{k['cursor_cast']})"
            else:
                rhs = f"(${idx}{k['cursor_cast']})"
            last = i == depth - 1
            op = ("<" if k["desc"] else ">") if last else "="
            terms.append(f"{k['order_expr']} {op} {rhs}")
            params.append(str(int(value)) if k["is_node"] else _json.dumps(value))
            idx += 1
        branches.append((" AND ".join(terms), params, idx))
    return branches


def _jsonb_text(value: Any) -> str:
    """A value as ``jsonb ->>`` prints it: text as-is, anything else as JSON.

    ``str()`` is not that form: it renders ``True`` where Postgres prints
    ``true``, so a boolean filter would never match.
    """
    return value if isinstance(value, str) else _json.dumps(value)


def _compile_scan_where(
    clauses: list[NodesScanWhere],
    *,
    next_param_idx: int,
) -> tuple[list[str], list[Any], int]:
    """Translate ``where`` clauses into agtype-native SQL fragments + params.

    All fragments operate on the raw ``v.properties`` agtype column.
    agtype's text form is JSON, so we JSON-encode each value (strings
    become quoted, numbers/booleans stay bare) and cast to agtype at
    query-binding time.  This lets the planner use the per-label
    btree indexes on ``(properties -> '"<key>"'::agtype)`` that
    graphos creates at tenant-registration time (notably the
    ``<Label>_id_unique`` index, which is what makes cursor pagination
    fast — the ``ORDER BY (properties -> '"id"'::agtype)`` becomes an
    index range scan instead of a parallel sort over the full table).

    Property names are validated by the Pydantic layer; direct
    interpolation of the key into the SQL fragment is safe.
    """
    fragments: list[str] = []
    params: list[Any] = []
    idx = next_param_idx
    for c in clauses:
        prop = c.property
        # All branches below speak the same JSONB-extract dialect —
        # ``((properties::text)::jsonb) ->> '<prop>'`` — so the planner
        # consistently picks the ``<Label>_<prop>_btree`` /
        # ``<Label>_<prop>_lower`` / ``<Label>_<prop>_trgm`` indexes
        # built by ``bootstrap.bootstrap_tenant`` /
        # ``resolver.indexer``.  The previous agtype-form predicates
        # (``properties -> '"<prop>"'::agtype``) were a different
        # functional expression that no index matched — every
        # /nodes:scan fell back to seq scan over the inheritance
        # hierarchy.  The ``op=in`` branch below and the /search
        # handler follow the same pattern.
        if c.op == "exists":
            fragments.append(
                f"((((v.properties)::text)::jsonb) ->> '{prop}') "
                f"IS NOT NULL"
            )
        elif c.op == "not exists":
            fragments.append(
                f"((((v.properties)::text)::jsonb) ->> '{prop}') "
                f"IS NULL"
            )
        elif c.op in ("=", "!="):
            if c.value is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"op '{c.op}' on '{prop}' requires a value",
                )
            sql_op = "=" if c.op == "=" else "<>"
            fragments.append(
                f"((((v.properties)::text)::jsonb) ->> '{prop}') "
                f"{sql_op} ${idx}::text"
            )
            # ``->>`` returns text — compare to the value's text form
            # as ``->>`` emits it (``true``, ``42``, ``1.5``).
            params.append(_jsonb_text(c.value))
            idx += 1
        elif c.op in ("in", "not in"):
            if not isinstance(c.value, list) or not c.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"op '{c.op}' on '{prop}' requires a non-empty "
                        "list value"
                    ),
                )
            # JSONB-extract form ``((properties::text)::jsonb) ->> '<prop>'``
            # exactly matches the functional btree the resolver indexer
            # builds via ``_btree_index`` — the planner picks the
            # ``<Label>_<prop>_btree`` index per inheritance child and
            # gets a sub-millisecond bitmap scan.  The previous
            # agtype-form ``(properties -> '"<prop>"'::agtype) IN (...)``
            # was a different functional expression and didn't match
            # any index — fallback was seq scan over every child
            # (~1.5 s on a large inheritance hierarchy, empirically).
            #
            # ``->>`` returns text uniformly: strings as-is, numbers /
            # booleans as JSON text.  The wire param is a plain
            # ``text[]`` in that same form, which PG can ``ANY()``
            # against the indexed text expression without an extra
            # coercion.
            sql_op = "<> ALL" if c.op == "not in" else "= ANY"
            fragments.append(
                f"((((v.properties)::text)::jsonb) ->> '{prop}') "
                f"{sql_op} (${idx}::text[])"
            )
            params.append([_jsonb_text(x) for x in c.value])
            idx += 1
        else:  # pragma: no cover — Literal type prevents this
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unsupported op '{c.op}'",
            )
    return fragments, params, idx


def _project_properties(
    raw: dict[str, Any],
    project: list[str] | None,
) -> dict[str, Any]:
    """Filter the property bag down to the requested projection."""
    if project is None:
        out: dict[str, Any] = {}
        if "id" in raw:
            out["id"] = raw["id"]
        return out
    if project == ["*"]:
        return raw
    keys = set(project) | {"id"}
    return {k: v for k, v in raw.items() if k in keys}


# A property filter converts each row's properties (agtype to text to jsonb),
# which the planner prices as one cheap operator. On a compact table it then
# walks the id index in order and filters, even for a few matches late in that
# order: on PostgreSQL 18 a scan for 649 of 100,000 rows dropped 15,604 rows in
# 334 ms, where the property indexes and a sort take 28 ms. Ten times the
# default prices the conversions near what they cost; a filter that matches
# most rows keeps the ordered walk (5 -> 15 ms).
_FILTER_OPERATOR_COST = "0.025"


@router.post(
    "/nodes:scan",
    response_model=NodesScanResponse,
    summary="Enumerate nodes by label(s) with optional filter + projection",
    description=(
        "Schema-agnostic batch read.  Returns every node matching "
        "`labels` (1+, OR-combined) AND every `where` clause (AND-"
        "combined), with optional property projection and opaque "
        "cursor pagination.  Each label is fully exhausted before "
        "moving to the next, in `order_by` order with the graphid "
        "breaking ties.  A label with child labels (`label_hierarchy`) "
        "includes the children's nodes, each reported under its own "
        "leaf label.  Use ``/search`` for substring matching — "
        "``/nodes:scan`` is structural enumeration only."
    ),
)
async def scan_nodes(
    tenant: Tenant,
    body: NodesScanRequest,
    request: Request,
    principal: ReadPrincipal,
) -> NodesScanResponse:
    _check_tenant(tenant, principal)

    # ``order_by=None`` is the "no sort" opt-out — caller never
    # paginates, just wants the rows back unordered.  This bypasses
    # the AGE-planner pessimisation on the default ``ORDER BY
    # (properties -> '"id"'::agtype)`` form which empirically blows up
    # to 37 s on large parent-label scans even with a sargable
    # ``name_lower`` btree available — the planner picks the
    # ``id_unique`` btree to satisfy the ORDER BY and seq-filters
    # name_lower.  See ``contracts.graph.NodesScanRequest.order_by``
    # docstring for the full rationale.
    #
    # Cursor pagination + no-sort is contradictory by definition, so
    # 422 a request that mixes them.
    skip_order_by = body.order_by is None
    if skip_order_by and body.cursor is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="cursor pagination requires order_by; pass an explicit sort key",
        )

    # Normalise order_by into:
    #   ``order_by_wire``  — opaque value carried verbatim in the cursor
    #                        (str for single-key, list[dict] for multi-key)
    #   ``sort_keys``      — ordered list of per-key SQL fragments + casts
    if skip_order_by:
        order_by_wire, sort_keys = None, []
    else:
        order_by_wire, sort_keys = _normalize_order_by(body.order_by)

    cursor_label: str | None = None
    cursor_values: list[Any] = []
    cursor_gid: int | None = None
    if body.cursor is not None:
        cursor_label, cursor_values, cursor_gid = _decode_scan_cursor(
            body.cursor, body.labels, order_by_wire,
        )

    # The graphid breaks ties between rows equal on every sort key: a
    # non-unique sort property, or the same ``id`` in a parent label and
    # one of its children (the unique ``id`` index is per table).  Not
    # needed when the sort is already by graphid.
    tiebreak = bool(sort_keys) and not any(k["is_node"] for k in sort_keys)
    page_keys = [*sort_keys, _NODE_SORT_KEY] if tiebreak else sort_keys

    items: list[NodesScanItem] = []
    next_cursor: str | None = None
    truncated = False
    accumulated_bytes = 0
    started = time.perf_counter()
    pool = request.app.state.pool

    if cursor_label is None:
        labels_to_scan = list(body.labels)
    else:
        skip = body.labels.index(cursor_label)
        labels_to_scan = list(body.labels[skip:])

    # Pre-build per-row SQL pieces from the normalised sort keys.
    # * order_by_sql: comma-separated ``expr [DESC]`` list — used in
    #   ORDER BY and (without DESC) for the cursor-position predicate.
    # * anchor_selects: one ``<expr>::text AS anchor_value_<i>_text``
    #   per key so the row carries each anchor value back separately.
    # * not_null_sql: AND of ``expr IS NOT NULL`` predicates so a
    #   row missing any sort key doesn't break lexicographic order.
    #
    # With ``skip_order_by`` (sort_keys=[]), all three collapse to
    # empty strings and the SQL builder below omits the corresponding
    # clauses — see ``sql`` template.
    order_by_sql = ", ".join(
        f"{k['order_expr']}{' DESC' if k['desc'] else ''}"
        for k in page_keys
    )
    anchor_selects = ", ".join(
        f"{k['anchor_select']} AS anchor_value_{i}_text"
        for i, k in enumerate(page_keys)
    )
    not_null_sql = " AND ".join(k["not_null"] for k in sort_keys)

    async def _scan_part(
        cursor_frag: str, cursor_params: list[Any], first_idx: int, label: str,
    ) -> bool:
        """Read one label (one cursor branch of it) into the page.

        Appends to ``items``, lowers ``remaining``, sets ``next_cursor``
        when the page fills; returns whether the payload cap was hit.
        """
        nonlocal remaining, accumulated_bytes, next_cursor
        nonlocal last_included_anchors, last_included_gid
        params: list[Any] = list(cursor_params)
        param_idx = first_idx
        clauses: list[str] = [cursor_frag] if cursor_frag else []

        where_frags, where_params, param_idx = _compile_scan_where(
            body.where, next_param_idx=param_idx,
        )
        clauses.extend(where_frags)
        params.extend(where_params)
        where_sql = (" AND ".join(clauses)) if clauses else "TRUE"

        # ``external_id`` keeps the graphos external-id convention
        # (``properties.id``) regardless of order_by — that's the
        # value callers navigate by in ``items[i].id``.  Each anchor
        # value is selected as its own column so we can encode them
        # back into the cursor as a typed list.
        #
        # When ``skip_order_by`` is set (caller passed ``order_by=None``)
        # the anchor select list, IS NOT NULL guard, and ORDER BY
        # clause are all omitted so the planner is free to pick the
        # selective WHERE-side index (e.g. our functional btree on
        # ``properties->>'name_lower'``) instead of being railroaded into
        # the ``id_unique`` btree by the ORDER BY-driven plan.
        #
        # ``leaf_label`` is the label of the table the row lives in,
        # which differs from ``label`` for a child label's row read
        # through its parent.  A scalar subquery rather than a join, so
        # it runs only for the rows returned and cannot change the plan
        # of the ordered, LIMITed scan.
        if skip_order_by:
            select_extras = ""
            order_by_clause = ""
            where_full = f"WHERE {where_sql}"
        else:
            select_extras = f"{anchor_selects},"
            order_by_clause = f"ORDER BY {order_by_sql}"
            where_full = f"WHERE {where_sql} AND {not_null_sql}"
        sql = f"""
            SELECT
                trim(both '"' from (v.properties -> '"id"'::agtype)::text)
                                              AS external_id,
                (SELECT c.relname FROM pg_class c WHERE c.oid = v.tableoid)
                                              AS leaf_label,
                {select_extras}
                v.properties::text            AS props_text
            FROM "{tenant}"."{label}" v
            {where_full}
            {order_by_clause}
            LIMIT ${param_idx};
        """
        params.append(remaining)

        try:
            async with pool.acquire() as conn, conn.transaction():
                if where_frags and not skip_order_by:
                    await conn.execute(f"SET LOCAL cpu_operator_cost = {_FILTER_OPERATOR_COST};")
                rows = await conn.fetch(sql, *params)
        except asyncpg.exceptions.UndefinedTableError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"label '{label}' not registered for tenant "
                    f"'{tenant}': {exc}"
                ),
            ) from exc

        for row in rows:
            props = _safe_decode_props(row["props_text"])
            projected = _project_properties(props, body.project)
            row_bytes = len(_json.dumps(projected).encode("utf-8"))
            if (
                items
                and accumulated_bytes + row_bytes > SCAN_PAYLOAD_CAP_BYTES
            ):
                # Cap hit — emit cursor pointing at the last *included*
                # row's anchor values.  With ``skip_order_by`` there is
                # no stable position to encode, so callers in that mode
                # see ``truncated=True, next_cursor=None`` — they opted
                # out of pagination by passing ``order_by=None``.
                if not skip_order_by:
                    next_cursor = _encode_scan_cursor(
                        label, order_by_wire, last_included_anchors,
                        last_included_gid,
                    )
                return True
            # Decode each anchor value back to a native Python type so
            # cursor encoding stays faithful across pages.
            # * ``_node`` (is_node) keys: column is bigint (graphid::text).
            # * Property keys: column is agtype text — JSON-shaped, so
            #   ``json.loads`` recovers str / int / float / bool.
            row_anchors: list[Any] = []
            for i, k in enumerate(page_keys):
                raw = row[f"anchor_value_{i}_text"]
                try:
                    if k["is_node"]:
                        row_anchors.append(int(raw))
                    else:
                        row_anchors.append(_json.loads(raw))
                except Exception:  # noqa: BLE001 — defensive
                    row_anchors.append(str(raw).strip('"'))
            items.append(NodesScanItem(
                id=str(row["external_id"]),
                label=str(row["leaf_label"]),
                properties=projected,
            ))
            if tiebreak:
                last_included_anchors = row_anchors[:-1]
                last_included_gid = row_anchors[-1]
            else:
                last_included_anchors = row_anchors
            accumulated_bytes += row_bytes
            remaining -= 1
            if remaining <= 0:
                # Same skip-cursor reasoning as the payload-cap path:
                # callers in ``skip_order_by`` mode opted out of
                # pagination, so emit ``next_cursor=None`` even when
                # the limit was exactly reached.
                if not skip_order_by:
                    next_cursor = _encode_scan_cursor(
                        label, order_by_wire, last_included_anchors,
                        last_included_gid,
                    )
                break

        return False

    remaining = body.limit
    # The most recently included row's sort-key values (one per key) and
    # graphid, for the cursor; kept across the cursor branches of a label.
    last_included_anchors: list[Any] = []
    last_included_gid: int | None = None
    for li, label in enumerate(labels_to_scan):
        if remaining <= 0 or truncated:
            break
        last_included_anchors, last_included_gid = [], None

        # The first label resumes after the cursor: rows tied with the
        # cursor's row first, then the rest, one query each
        # (``_cursor_branches``); any other label is read from its start.
        # A cursor without a graphid (issued before the tiebreaker)
        # resumes on the sort keys alone.
        cursor_parts: list[tuple[str, list[Any], int]] = [("", [], 1)]
        if li == 0 and cursor_values:
            if tiebreak and cursor_gid is not None:
                pred_keys = page_keys
                pred_values = [*cursor_values, cursor_gid]
            else:
                pred_keys, pred_values = sort_keys, cursor_values
            cursor_parts = _cursor_branches(pred_keys, pred_values, next_param_idx=1)
        for cursor_frag, cursor_params, first_idx in cursor_parts:
            if remaining <= 0 or truncated:
                break
            truncated = await _scan_part(
                cursor_frag, cursor_params, first_idx, label,
            )

        if remaining <= 0 or truncated:
            break

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return NodesScanResponse(
        tenant=tenant,
        labels=body.labels,
        items=items,
        next_cursor=next_cursor,
        scanned=len(items),
        truncated_by_payload_cap=truncated,
        elapsed_ms=elapsed_ms,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  POST /edges:scan — bulk edge enumeration
# ═══════════════════════════════════════════════════════════════════════════
#
# Symmetric to ``/nodes:scan`` but over edges.  Order is fixed in v1
# by edge graphid (AGE does not index it) — edges don't have a consumer-
# assigned external id the way nodes do, so multi-key / property-based
# sort would need consumer-declared functional indexes on edge tables.
# That's a v2 extension; not breaking the v1 contract.
#
# Each row resolves both endpoints to ``(external_id, label)`` against
# the parent vertex inheritance table, for the page's edges only (see
# ``_edge_scan_page_sql``) — matches the input shape of ``/edges:bulk``
# so callers can round-trip without translation.


def _compile_edge_scan_where(
    clauses: list[NodesScanWhere],
    *,
    next_param_idx: int,
) -> tuple[list[str], list[Any], int]:
    """Same as ``_compile_scan_where`` but against ``e.properties`` (the
    edge table's properties column) instead of ``v.properties``."""
    fragments: list[str] = []
    params: list[Any] = []
    idx = next_param_idx
    for c in clauses:
        prop = c.property
        if c.op == "exists":
            fragments.append(
                f"(e.properties -> '\"{prop}\"'::agtype) IS NOT NULL"
            )
        elif c.op == "not exists":
            fragments.append(
                f"(e.properties -> '\"{prop}\"'::agtype) IS NULL"
            )
        elif c.op in ("=", "!="):
            if c.value is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"op '{c.op}' on '{prop}' requires a value",
                )
            sql_op = "=" if c.op == "=" else "!="
            fragments.append(
                f"(e.properties -> '\"{prop}\"'::agtype) "
                f"{sql_op} (${idx}::text::agtype)"
            )
            params.append(_json.dumps(c.value))
            idx += 1
        elif c.op in ("in", "not in"):
            if not isinstance(c.value, list) or not c.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"op '{c.op}' on '{prop}' requires a non-empty "
                        "list value"
                    ),
                )
            negate = "NOT " if c.op == "not in" else ""
            fragments.append(
                f"{negate}((e.properties -> '\"{prop}\"'::agtype) IN ("
                f"SELECT x::agtype FROM unnest(${idx}::text[]) AS x))"
            )
            params.append([_json.dumps(x) for x in c.value])
            idx += 1
        else:  # pragma: no cover
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unsupported op '{c.op}'",
            )
    return fragments, params, idx


def _encode_edge_scan_cursor(edge_type: str, edge_gid: int) -> str:
    """Opaque cursor: ``{type, gid}`` base64-JSON."""
    payload = _json.dumps(
        {"type": edge_type, "gid": int(edge_gid)},
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_edge_scan_cursor(
    token: str, allowed_types: list[str],
) -> tuple[str, int]:
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        obj = _json.loads(raw.decode("utf-8"))
        edge_type = obj["type"]
        edge_gid = int(obj["gid"])
    except Exception as exc:  # noqa: BLE001 — any decode failure is a 400
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid cursor: {exc.__class__.__name__}",
        ) from exc
    if not isinstance(edge_type, str) or edge_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"cursor type '{edge_type}' is not in the request's "
                f"types {allowed_types}"
            ),
        )
    return edge_type, edge_gid


def _edge_scan_page_sql(
    tenant: str, edge_type: str, where_sql: str, *, limit_param: int,
) -> str:
    """One ``/edges:scan`` page: the edges first, then their endpoints.

    ``page`` takes the next ``limit`` edges in graphid order; ``ends``
    resolves only those edges' endpoints to ``(external id, label)``.
    Joining the whole edge table to ``_ag_label_vertex`` before the
    LIMIT, as a single flat query does, makes the planner hash every
    edge against every vertex: minutes and gigabytes of temp files on
    a large tenant, for a page of 50.  Both CTEs are MATERIALIZED so
    the planner cannot fold the LIMIT back behind the joins.

    Endpoints are LEFT JOINed: an edge whose endpoint vertex no longer
    exists comes back with NULL labels, and the caller skips it.
    """
    return f"""
        WITH page AS MATERIALIZED (
            SELECT e.id, e.start_id, e.end_id, e.properties
            FROM "{tenant}"."{edge_type}" e
            WHERE {where_sql}
            ORDER BY e.id
            LIMIT ${limit_param}
        ),
        ends AS MATERIALIZED (
            SELECT v.id, v.properties -> '"id"'::agtype AS ext_id, v.tableoid
            FROM "{tenant}"."_ag_label_vertex" v
            WHERE v.id IN (SELECT start_id FROM page UNION SELECT end_id FROM page)
        )
        SELECT
            p.id::text                                AS edge_gid,
            trim(both '"' from s.ext_id::text)        AS src_id,
            c_src.relname                             AS src_label,
            trim(both '"' from d.ext_id::text)        AS dst_id,
            c_dst.relname                             AS dst_label,
            p.properties::text                        AS props_text
        FROM page p
        LEFT JOIN ends s ON s.id = p.start_id
        LEFT JOIN ends d ON d.id = p.end_id
        LEFT JOIN pg_class c_src ON c_src.oid = s.tableoid
        LEFT JOIN pg_class c_dst ON c_dst.oid = d.tableoid
        ORDER BY p.id;
    """


@router.post(
    "/edges:scan",
    response_model=EdgesScanResponse,
    summary="Enumerate edges by type with optional property filter + projection",
    description=(
        "Schema-agnostic batch read of edges.  Returns every edge "
        "whose ``type`` is in ``types`` AND that matches every "
        "``where`` clause, with optional property projection and "
        "opaque cursor pagination.  Each item carries resolved "
        "``(src_id, src_label, dst_id, dst_label)`` so callers can "
        "round-trip directly through ``/edges:bulk``.  Order is fixed "
        "in v1 by edge graphid PK; consumer-declared edge-property "
        "indexes + multi-key sort can be added later without breaking "
        "the v1 contract."
    ),
)
async def scan_edges(
    tenant: Tenant,
    body: EdgesScanRequest,
    request: Request,
    principal: ReadPrincipal,
) -> EdgesScanResponse:
    _check_tenant(tenant, principal)

    cursor_type: str | None = None
    cursor_gid: int | None = None
    if body.cursor is not None:
        cursor_type, cursor_gid = _decode_edge_scan_cursor(
            body.cursor, body.types,
        )

    items: list[EdgesScanItem] = []
    next_cursor: str | None = None
    truncated = False
    accumulated_bytes = 0
    started = time.perf_counter()
    pool = request.app.state.pool

    if cursor_type is None:
        types_to_scan = list(body.types)
    else:
        skip = body.types.index(cursor_type)
        types_to_scan = list(body.types[skip:])

    remaining = body.limit
    for ti, edge_type in enumerate(types_to_scan):
        if remaining <= 0 or truncated:
            break

        # Position along the edge graphid order: the request cursor for
        # the first type, then the last edge fetched, page after page.
        after_gid: int | None = cursor_gid if ti == 0 else None
        last_edge_gid: int | None = None
        while remaining > 0 and not truncated:
            params: list[Any] = []
            param_idx = 1
            clauses: list[str] = []
            if after_gid is not None:
                clauses.append(
                    f"e.id > agtype_to_graphid(${param_idx}::text::agtype)"
                )
                params.append(str(int(after_gid)))
                param_idx += 1

            where_frags, where_params, param_idx = _compile_edge_scan_where(
                body.where, next_param_idx=param_idx,
            )
            clauses.extend(where_frags)
            params.extend(where_params)
            where_sql = (" AND ".join(clauses)) if clauses else "TRUE"

            page_size = remaining
            sql = _edge_scan_page_sql(
                tenant, edge_type, where_sql, limit_param=param_idx,
            )
            params.append(page_size)

            try:
                async with pool.acquire() as conn, conn.transaction():
                    # The endpoint lookup appends every vertex label table;
                    # on tenants never ANALYZEd the planner's row estimates
                    # are inflated enough to trigger JIT, which then spends
                    # seconds compiling a query that runs in milliseconds.
                    await conn.execute("SET LOCAL jit = off;")
                    rows = await conn.fetch(sql, *params)
            except asyncpg.exceptions.UndefinedTableError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"edge type '{edge_type}' not registered for tenant "
                        f"'{tenant}': {exc}"
                    ),
                ) from exc

            for row in rows:
                edge_gid = int(row["edge_gid"])
                after_gid = edge_gid
                if row["src_label"] is None or row["dst_label"] is None:
                    # An endpoint vertex no longer exists: such edges were
                    # never returned, and still are not.
                    continue
                props = _safe_decode_props(row["props_text"])
                projected = _project_properties(props, body.project)
                row_bytes = len(_json.dumps(projected).encode("utf-8")) + 64
                if (
                    items
                    and accumulated_bytes + row_bytes > SCAN_PAYLOAD_CAP_BYTES
                ):
                    if last_edge_gid is not None:
                        next_cursor = _encode_edge_scan_cursor(
                            edge_type, last_edge_gid,
                        )
                    truncated = True
                    break
                items.append(EdgesScanItem(
                    src_id=str(row["src_id"]),
                    src_label=str(row["src_label"]),
                    dst_id=str(row["dst_id"]),
                    dst_label=str(row["dst_label"]),
                    type=edge_type,
                    properties=projected,
                ))
                last_edge_gid = edge_gid
                accumulated_bytes += row_bytes
                remaining -= 1
                if remaining <= 0:
                    next_cursor = _encode_edge_scan_cursor(edge_type, edge_gid)
                    break

            if len(rows) < page_size:
                break  # this edge type is exhausted

        if remaining <= 0 or truncated:
            break

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return EdgesScanResponse(
        tenant=tenant,
        types=body.types,
        items=items,
        next_cursor=next_cursor,
        scanned=len(items),
        truncated_by_payload_cap=truncated,
        elapsed_ms=elapsed_ms,
    )


# ── Schema-driven edge resolver ─────────────────────────────────────────────
#
# ``POST /v1/{tenant}/graph/edges:resolve`` — compile each registered
# ``resolve_rule`` to a parameterised INSERT and execute it server-side.
# Replaces the consumer-side N×N property-match loop for any tenant
# whose schema declares the rule.  See ``docs/features.md`` for the
# rule format and ``graphos.resolver.compiler`` for the compilation
# contract.


@router.post(
    "/edges:resolve",
    response_model=ResolveEdgesResponse,
    summary="Compile + execute registered resolve_rules for one or more edge types",
    description=(
        "For each requested edge type that has a registered "
        "``resolve_rule`` on the tenant's schema, compile the rule to a "
        "parameterised INSERT and execute it server-side.  Returns "
        "per-type edges-written counts.  Idempotent via "
        "``ON CONFLICT (start_id, end_id) DO NOTHING`` — re-running for "
        "an unchanged graph is a no-op.  With ``Prefer: respond-async`` it "
        "runs as a background job (202, see ``/v1/jobs``)."
    ),
    responses=ACCEPTED_RESPONSE,
)
async def resolve_edges(
    tenant: Tenant,
    body: ResolveEdgesRequest,
    request: Request,
    principal: WritePrincipal,
) -> ResolveEdgesResponse | JSONResponse:
    _check_tenant(tenant, principal)
    _resolve_plan(tenant, body)   # reject unknown edge types before queueing
    if wants_async(request):
        return await respond_async(
            request, kind="resolve_edges", tenant=tenant, body=body,
            principal=principal,
        )
    return await run_sync(
        request, kind="resolve_edges", tenant=tenant, body=body, principal=principal,
        run=lambda: run_resolve(request.app.state.pool, tenant, body),
    )


def _resolve_plan(
    tenant: str, body: ResolveEdgesRequest,
) -> tuple[dict[str, list[dict[str, object]]], list[str]]:
    """The tenant's rules grouped by edge type, and the types to run."""
    spec = registered_tenants()[tenant]

    # Build the candidate edge-type list.  ``spec.resolve_rules`` is a
    # tuple of ``(edge_type, rule_dict)`` pairs persisted at
    # registration.  Multiple pairs MAY share an edge_type — each
    # represents an independent rule (e.g. two different ways to derive
    # ``RELATES_TO`` edges from different src-side array stamps).  We
    # group them into a list-per-type so the endpoint iterates over
    # every rule that matches the requested edge_type.
    rules_by_type: dict[str, list[dict[str, object]]] = defaultdict(list)
    for et, rule in spec.resolve_rules:
        rules_by_type[et].append(dict(rule))

    if body.edge_types is None:
        requested = list(rules_by_type.keys())
    else:
        requested = list(body.edge_types)
        # Reject up-front if any requested type lacks a rule —
        # otherwise the caller sees a silent zero and might assume
        # the rule fired.
        missing = [t for t in requested if t not in rules_by_type]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"no resolve_rule registered for edge_type(s) "
                    f"{missing!r} on tenant {tenant!r}.  "
                    f"Re-POST /v1/tenants with `resolve_rule` set on each."
                ),
            )
    return rules_by_type, requested


def _rule_properties(
    rules_by_type: dict[str, list[dict[str, Any]]], edge_types: list[str],
) -> set[str]:
    """Properties read by the rules of ``edge_types``.  A rule outside the
    DSL contributes none; compiling it reports the error."""
    out: set[str] = set()
    for edge_type in edge_types:
        for rule in rules_by_type[edge_type]:
            with contextlib.suppress(CompileError):
                out |= rule_property_names(edge_type, rule)
    return out


async def run_resolve(
    pool: asyncpg.Pool, tenant: str, body: ResolveEdgesRequest,
) -> ResolveEdgesResponse:
    """The resolution itself, for the endpoint and for background jobs."""
    rules_by_type, requested = _resolve_plan(tenant, body)
    # With strict_endpoints, rules only link the label pairs their type declares.
    policy = policy_for(registered_tenants()[tenant])

    # Catalogue drives the FROM-list label inference.  Looked up for the
    # rules' own properties only, from the database now (one GIN probe per
    # label and property), so labels and properties written a moment ago
    # count.
    catalogue = await catalogue_for_properties(
        pool, tenant, _rule_properties(rules_by_type, requested),
    )

    total_started = time.perf_counter()

    # Parallelise per-edge-type work.  The previous sequential loop made
    # the wall time the sum of every rule's INSERT cost — on a large
    # tenant that was ~11 min for 13 rules even though each rule sits on its own
    # label tables and shares no row-level contention with the others.
    # Each task acquires its own connection from the pool, so the only
    # bound we set ourselves is the semaphore cap below.
    #
    # The cap (``RESOLVE_PARALLELISM``) is sized for two pressures that
    # are NOT visible at the asyncpg layer:
    #   1. postgres shared-memory segments — the planner allocates work
    #      memory per active backend; resolve INSERTs on a large tenant
    #      that run a full label-pair cross-product can each consume
    #      hundreds of MiB.  Running all 13 rules at once OOMs the
    #      segment (observed empirically).
    #   2. parallel-worker contention — postgres already auto-parallelises
    #      individual INSERTs; stacking 13 of those on top of each other
    #      saturates the worker pool and the rules just queue inside
    #      postgres anyway.
    # A cap of 4 keeps each backend with enough work_mem headroom and
    # leaves CPU for the auto-parallel workers inside each INSERT.
    RESOLVE_PARALLELISM = 4
    sem = asyncio.Semaphore(RESOLVE_PARALLELISM)

    async def _run_one(edge_type: str) -> ResolveEdgesResult:
        rules = rules_by_type[edge_type]
        written = 0
        warning: str | None = None
        no_label_count = 0
        undeclared = 0

        async with sem:
            # Start timing AFTER acquiring the semaphore so the
            # reported ``duration_ms`` reflects this edge_type's actual
            # compile + INSERT work, not the time it spent waiting in
            # the fan-out queue.  Without this, every rule in the
            # second wave of the semaphore showed the first wave's
            # wall as its own duration — even rules that did zero
            # work because no label carried their src property.
            et_started = time.perf_counter()
            try:
                compiled = compile_resolve_rules_for_edge_type(
                    tenant=tenant,
                    edge_type=edge_type,
                    rules=rules,
                    catalogue=catalogue,
                    allows=(
                        (lambda s, d, et=edge_type: policy.allows(et, s, d))
                        if policy.strict else None
                    ),
                )
            except CompileError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"compile error for edge_type {edge_type!r}: {exc}"
                    ),
                ) from exc

            no_label_count = compiled.no_label_rule_count
            undeclared = compiled.undeclared_arm_count
            # One INSERT per edge_type: every (src_label, dst_label) arm of
            # every rule is UNION ALL'd into a single statement, so postgres
            # plans globally and we pay one round-trip.  Bounded at 30 min
            # so a runaway plan still fails.
            try:
                written += await writes.insert_resolved_edges(
                    pool, tenant=tenant, compiled=compiled, timeout_s=1800.0,
                )
            except CypherError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"resolve INSERT failed for edge_type "
                        f"{edge_type!r} "
                        f"({compiled.arm_count} arms): {exc}"
                    ),
                ) from exc

        if no_label_count == len(rules) and rules:
            warning = (
                "no vertex label currently carries the properties "
                "referenced by this rule; rule remained registered."
            )
        elif undeclared:
            warning = (
                f"{undeclared} label pair(s) left out: not declared for "
                f"{edge_type} (strict_endpoints)."
            )

        et_elapsed = int((time.perf_counter() - et_started) * 1000)
        return ResolveEdgesResult(
            edge_type=edge_type,
            edges_written=written,
            duration_ms=et_elapsed,
            warning=warning,
        )

    # ``gather`` preserves submission order; ``return_exceptions=False``
    # cancels siblings on the first failure, which is what we want — a
    # compile / INSERT error means the caller's request is malformed
    # and continuing would just produce more confusing partial state.
    results = list(await asyncio.gather(*(_run_one(et) for et in requested)))
    total_written = sum(r.edges_written for r in results)

    total_elapsed = int((time.perf_counter() - total_started) * 1000)
    return ResolveEdgesResponse(
        tenant=tenant,
        results=results,
        total_edges_written=total_written,
        total_duration_ms=total_elapsed,
    )

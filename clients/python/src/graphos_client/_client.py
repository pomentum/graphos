"""Async HTTP client for graphos."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Mapping
from http import HTTPStatus
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from graphos_client._models import (
    BulkEdgesResponse,
    BulkNodesResponse,
    CacheGetResponse,
    CachePutResponse,
    CommunitiesResponse,
    CommunityIdsResponse,
    CommunitySummary,
    DeleteByPropertyResponse,
    DeleteEdgeResponse,
    DeleteNodeResponse,
    EdgeGetResponse,
    EdgesAggregateResponse,
    EdgesScanResponse,
    EdgesUpsertResponse,
    EdgeUpsert,
    ExpandResponse,
    GraphologyExport,
    HistogramResponse,
    InducedEdgesResponse,
    Job,
    NeighborsResponse,
    NodeGetResponse,
    NodePropertiesUpsertResponse,
    NodesAggregateResponse,
    NodesScanResponse,
    NodesUpsertResponse,
    NodeUpsert,
    PathsResponse,
    RecomputeCommunitiesResponse,
    SampleSubgraphResponse,
    SearchResponse,
    TenantInfo,
    TenantStatsResponse,
    TopByEdgeResponse,
)

log = logging.getLogger(__name__)

_MERGE_PATCH = "application/merge-patch+json"


def _label_param(label: str | None) -> dict[str, str] | None:
    return None if label is None else {"label": label}


def _if_match_header(if_match: str | None) -> dict[str, str] | None:
    return None if if_match is None else {"If-Match": if_match}


# Sentinel for "argument not passed" — distinguished from explicit
# ``None`` (which has a documented meaning of "opt out of ORDER BY"
# on ``scan_nodes``).  See ``scan_nodes`` docstring.
_UNSET: Any = object()


class GraphosError(RuntimeError):
    """Wraps a non-2xx response from graphos with the status code + detail."""

    def __init__(self, status_code: int, detail: str, method: str, url: str):
        super().__init__(f"{method} {url} → {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.method = method
        self.url = url


class GraphosNotReady(GraphosError):
    """Raised on 503 — graphos-db unreachable or lifespan incomplete."""


class GraphosClient:
    """Async HTTP client for the graphos service.

    Use as a context manager so the underlying httpx pool gets closed::

        async with GraphosClient(base_url=..., token=..., tenant=...) as cli:
            await cli.search(...)

    Or hold the client across many calls and ``close()`` explicitly.
    """

    def __init__(
        self,
        *,
        base_url: str,
        tenant: str,
        token: str | None = None,
        timeout_s: float = 30.0,
        retries: int = 3,
        retry_backoff_s: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Construct the client.

        ``token`` is optional.  When omitted or empty, no Authorization
        header is sent — appropriate for dev deployments where graphos
        runs without an auth-tokens file (anonymous = admin).  Real
        deployments pass a bearer token; the server's TokenStore maps
        it to the consumer's tenant + admin scope.
        """
        self._base_url = base_url.rstrip("/")
        self._token = token or None
        self._tenant = tenant
        self._retries = retries
        self._retry_backoff_s = retry_backoff_s
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        # Explicit connection-pool limits — without these httpx defaults
        # to no upper-bound on the keepalive pool but still applies a
        # per-call connect deadline.  A typical read path issues many
        # short calls per request (seed lookup, community-id,
        # expansion, enrichment); pooled connections eliminate
        # the 30-80 ms TCP+TLS handshake each time a fresh socket would
        # have been opened to graphos-api.  ``keepalive_expiry`` is
        # generous so the pool survives think-time between queries.
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_s),
            headers=headers,
            limits=httpx.Limits(
                max_keepalive_connections=10,
                max_connections=20,
                # Was 30.0 — dropped to 10 s to match the
                # MetadataClient guard.  Idle sockets that sit in the
                # pool past server-side TCP idle-close (varies by
                # reverse-proxy / server config, typically 30–60 s)
                # get RST'd server-side; the client doesn't observe
                # the close until next use, then the next request
                # hangs through the full read-timeout window (~30 s)
                # before httpx surfaces a ``ReadTimeout``.  Dropping
                # idle connections after 10 s keeps the pool fresh
                # well below typical server-side idle-close
                # thresholds; the small re-handshake cost on
                # subsequent calls is far cheaper than the 30+ s
                # ReadTimeout-then-retry amplification it prevents.
                keepalive_expiry=10.0,
            ),
            # Optional custom transport (tests: httpx.MockTransport; proxies, TLS).
            transport=transport,
        )

    async def __aenter__(self) -> GraphosClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    # ── internal HTTP helpers ──────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        content: bytes | str | None = None,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = True,
    ) -> httpx.Response:
        """Send a request with retry on transient failures.

        Retries network errors and 5xx responses (except 504, a statement
        timeout that would only time out again) when ``idempotent`` is true.
        Long-running operations pass ``idempotent=False`` so a failure is
        never re-run automatically.  4xx errors surface immediately as
        ``GraphosError``.
        """
        url = path
        attempt = 0
        while True:
            attempt += 1
            try:
                request_headers = dict(headers or {})
                if content_type:
                    request_headers["content-type"] = content_type
                resp = await self._http.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    content=content,
                    headers=request_headers or None,
                )
            except (
                httpx.NetworkError,
                httpx.ConnectError,
                httpx.ReadError,
                httpx.WriteError,
                httpx.RemoteProtocolError,
                httpx.PoolTimeout,
                httpx.ConnectTimeout,
            ) as exc:
                if idempotent and attempt <= self._retries:
                    await asyncio.sleep(self._retry_backoff_s * (2 ** (attempt - 1)))
                    continue
                raise GraphosError(0, f"network error: {exc}", method, url) from exc
            except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
                # ``Read``/``WriteTimeout`` are intentionally NOT
                # retried.  Semantically they mean "the server
                # received our request and we waited the configured
                # ``read``/``write`` window for the response".
                # Retrying compounds latency on a possibly-wedged
                # keepalive socket without certainty of progress;
                # a bulk-load job issues many sequential bulk
                # calls, so 3 retries × 30 s each across several
                # hung calls in a row stalls the job for 10–30 min
                # before any outer deadline can act.  Surface the
                # timeout immediately as a network error and let
                # the caller's bounded-deadline / retry layer
                # decide.
                raise GraphosError(
                    0,
                    f"{type(exc).__name__}: {exc}",
                    method, url,
                ) from exc

            if resp.status_code < httpx.codes.BAD_REQUEST:
                return resp
            if (
                resp.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
                and resp.status_code != httpx.codes.GATEWAY_TIMEOUT
                and idempotent
                and attempt <= self._retries
            ):
                await asyncio.sleep(self._retry_backoff_s * (2 ** (attempt - 1)))
                continue

            detail = _extract_detail(resp)
            if resp.status_code == httpx.codes.SERVICE_UNAVAILABLE:
                raise GraphosNotReady(resp.status_code, detail, method, url)
            raise GraphosError(resp.status_code, detail, method, url)

    # ── health & info ──────────────────────────────────────────────────────

    async def healthz(self) -> dict[str, Any]:
        resp = await self._request("GET", "/healthz")
        return resp.json()

    async def readyz(self) -> dict[str, Any]:
        resp = await self._request("GET", "/readyz")
        return resp.json()

    async def info(self) -> dict[str, Any]:
        resp = await self._request("GET", "/v1/info")
        return resp.json()

    # ── tenants ────────────────────────────────────────────────────────────

    async def list_tenants(self) -> list[TenantInfo]:
        resp = await self._request("GET", "/v1/tenants")
        return [TenantInfo.model_validate(t) for t in resp.json()["tenants"]]

    async def get_tenant(self, name: str | None = None) -> TenantInfo:
        target = name or self._tenant
        resp = await self._request("GET", f"/v1/tenants/{target}")
        return TenantInfo.model_validate(resp.json())

    async def register_tenant(
        self,
        *,
        name: str | None = None,
        vertex_labels: Iterable[str] = (),
        edge_labels: Iterable[str | Mapping[str, Any]] = (),
        indexes: Iterable[str] = (),
        generated_columns: Iterable[str] = (),
        label_hierarchy: Mapping[str, str] | None = None,
        meta: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Register / re-register a tenant.

        ``edge_labels`` entries are names or ``{"name": ..., "resolve_rule": {...}}``
        dicts (rule-based edges).  ``label_hierarchy`` maps ``{child_label: parent_label}``;
        graphos wires postgres table inheritance so writes under the
        child label are visible to ``MATCH (n:parent)`` queries
        without storing duplicate rows.  Both child and parent must
        appear in ``vertex_labels``.  Acyclic.
        """
        body = {
            "name": name or self._tenant,
            "vertex_labels": list(vertex_labels),
            "edge_labels": list(edge_labels),
            "indexes": list(indexes),
            "generated_columns": list(generated_columns),
            "label_hierarchy": dict(label_hierarchy or {}),
            "meta": dict(meta or {}),
        }
        resp = await self._request("POST", "/v1/tenants", json_body=body)
        return resp.json()

    async def unregister_tenant(self, name: str | None = None) -> dict[str, Any]:
        target = name or self._tenant
        resp = await self._request("DELETE", f"/v1/tenants/{target}")
        return resp.json()

    async def resolve_edges(
        self,
        *,
        edge_types: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/{tenant}/graph/edges:resolve``.

        Trigger server-side cross-property edge derivation for every
        edge label registered with a ``resolve_rule``.  See
        ``graphos.contracts.tenants.ResolveEdgesRequest`` for the body
        shape and ``ResolveEdgesResponse`` for the result.

        ``edge_types`` selects a subset of registered types (each must
        already have a rule on the tenant spec); omit to resolve all
        registered types.

        Synchronous — each rule compiles to one parameterised INSERT,
        wall-clock budget is seconds even on large tenants.  Idempotent
        via ``ON CONFLICT (start_id, end_id) DO NOTHING``.
        """
        resp = await self._request(
            "POST",
            f"/v1/{self._tenant}/graph/edges:resolve",
            json_body=_resolve_body(edge_types),
            idempotent=False,   # long-running: never re-run on a 5xx
        )
        return resp.json()

    async def submit_resolve_edges(self, *, edge_types: Iterable[str] | None = None) -> Job:
        """:meth:`resolve_edges` as a background job; see :meth:`wait_for_job`."""
        return await self._submit(
            f"/v1/{self._tenant}/graph/edges:resolve", _resolve_body(edge_types),
        )

    # ── writes ─────────────────────────────────────────────────────────────

    async def upsert_nodes(self, nodes: Iterable[NodeUpsert]) -> NodesUpsertResponse:
        body = {"nodes": [n.model_dump() for n in nodes]}
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/graph/nodes", json_body=body,
        )
        return NodesUpsertResponse.model_validate(resp.json())

    async def upsert_edges(self, edges: Iterable[EdgeUpsert]) -> EdgesUpsertResponse:
        body = {"edges": [e.model_dump() for e in edges]}
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/graph/edges", json_body=body,
        )
        return EdgesUpsertResponse.model_validate(resp.json())

    async def bulk_upsert_nodes(
        self, nodes: Iterable[NodeUpsert],
    ) -> BulkNodesResponse:
        ndjson = "\n".join(json.dumps(n.model_dump()) for n in nodes)
        resp = await self._request(
            "POST",
            f"/v1/{self._tenant}/graph/nodes:bulk",
            content=ndjson.encode("utf-8"),
            content_type="application/x-ndjson",
        )
        return BulkNodesResponse.model_validate(resp.json())

    async def bulk_upsert_edges(
        self, edges: Iterable[EdgeUpsert],
    ) -> BulkEdgesResponse:
        ndjson = "\n".join(json.dumps(e.model_dump()) for e in edges)
        resp = await self._request(
            "POST",
            f"/v1/{self._tenant}/graph/edges:bulk",
            content=ndjson.encode("utf-8"),
            content_type="application/x-ndjson",
        )
        return BulkEdgesResponse.model_validate(resp.json())

    async def snapshot_import(
        self,
        nodes: Iterable[NodeUpsert] = (),
        edges: Iterable[EdgeUpsert] = (),
    ) -> dict[str, Any]:
        """``POST .../graph/snapshot-import``: nodes and edges in one stream.

        Nodes are sent first; the server also flushes buffered nodes before
        every edge batch.  Returns the raw response (``nodes_upserted``,
        ``edges_upserted``, ``edges_skipped``, ``errors``, timings).  Not
        retried automatically: a large import is not something to repeat
        blindly on a transient error (re-sending it is safe, though).
        """
        lines = [json.dumps({"_kind": "node", **n.model_dump()}) for n in nodes]
        lines += [json.dumps({"_kind": "edge", **e.model_dump()}) for e in edges]
        resp = await self._request(
            "POST",
            f"/v1/{self._tenant}/graph/snapshot-import",
            content="\n".join(lines).encode("utf-8"),
            content_type="application/x-ndjson",
            idempotent=False,
        )
        return resp.json()

    async def scan_nodes(
        self,
        *,
        labels: list[str],
        where: list[dict[str, Any]] | None = None,
        project: list[str] | None = None,
        limit: int = 500,
        cursor: str | None = None,
        order_by: Any = _UNSET,
    ) -> NodesScanResponse:
        """POST /v1/{tenant}/graph/nodes:scan — one page.

        Schema-agnostic bulk enumeration.  See the service contract for
        the full body shape.  ``where`` items are dicts of
        ``{property, op, value}``; ``op`` is one of ``=``, ``!=``,
        ``in``, ``not in``, ``exists``, ``not exists``.

        ``order_by`` is opt-in.  Omit it to let the server apply its
        default (``"id"``).  Pass ``None`` to explicitly skip ORDER
        BY — required to dodge AGE's planner pessimisation on large-
        label scans whose WHERE targets a different functional
        index (the WHERE-side btree gets ignored in favour of the
        ``id_unique`` btree, taking 37 s vs 235 ms).  Pass a property
        name or list of clauses for an explicit sort.

        For multi-page scans prefer :meth:`scan_nodes_iter` which hides
        the cursor round-trip behind an async iterator.
        """
        body: dict[str, Any] = {"labels": labels, "limit": limit}
        if where is not None:
            body["where"] = where
        if project is not None:
            body["project"] = project
        if cursor is not None:
            body["cursor"] = cursor
        if order_by is not _UNSET:
            body["order_by"] = order_by
        resp = await self._request(
            "POST",
            f"/v1/{self._tenant}/graph/nodes:scan",
            json_body=body,
        )
        return NodesScanResponse.model_validate(resp.json())

    async def scan_nodes_iter(
        self,
        *,
        labels: list[str],
        where: list[dict[str, Any]] | None = None,
        project: list[str] | None = None,
        page_size: int = 500,
    ):
        """Async iterator over /nodes:scan results — hides cursor pagination.

        Yields :class:`NodesScanItem` one at a time.  Internally
        round-trips the cursor until the server reports ``next_cursor
        is None``.  Use this for full-graph walks; for single-page
        reads call :meth:`scan_nodes` directly.
        """
        cursor: str | None = None
        while True:
            page = await self.scan_nodes(
                labels=labels,
                where=where,
                project=project,
                limit=page_size,
                cursor=cursor,
            )
            for item in page.items:
                yield item
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    async def scan_edges(
        self,
        *,
        types: list[str],
        where: list[dict[str, Any]] | None = None,
        project: list[str] | None = None,
        limit: int = 500,
        cursor: str | None = None,
    ) -> EdgesScanResponse:
        """POST /v1/{tenant}/graph/edges:scan — one page.

        Returns edges with endpoint info resolved (``src_id``/``src_label``,
        ``dst_id``/``dst_label``) so callers can round-trip the result
        through ``/edges:bulk`` if they need to amend properties.
        """
        body: dict[str, Any] = {"types": types, "limit": limit}
        if where is not None:
            body["where"] = where
        if project is not None:
            body["project"] = project
        if cursor is not None:
            body["cursor"] = cursor
        resp = await self._request(
            "POST",
            f"/v1/{self._tenant}/graph/edges:scan",
            json_body=body,
        )
        return EdgesScanResponse.model_validate(resp.json())

    async def scan_edges_iter(
        self,
        *,
        types: list[str],
        where: list[dict[str, Any]] | None = None,
        project: list[str] | None = None,
        page_size: int = 500,
    ):
        """Async iterator over /edges:scan results."""
        cursor: str | None = None
        while True:
            page = await self.scan_edges(
                types=types,
                where=where,
                project=project,
                limit=page_size,
                cursor=cursor,
            )
            for item in page.items:
                yield item
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    # ── reads ──────────────────────────────────────────────────────────────

    async def search(
        self,
        q: str,
        *,
        label: str | None = None,
        limit: int = 20,
    ) -> SearchResponse:
        params: dict[str, Any] = {"q": q, "limit": limit}
        if label is not None:
            params["label"] = label
        resp = await self._request(
            "GET", f"/v1/{self._tenant}/graph/search", params=params,
        )
        return SearchResponse.model_validate(resp.json())

    async def neighbors(
        self,
        node_id: str,
        *,
        rel_types: list[str] | None = None,
        limit: int = 50,
    ) -> NeighborsResponse:
        params: dict[str, Any] = {"node_id": node_id, "limit": limit}
        if rel_types:
            params["rel_types"] = rel_types
        resp = await self._request(
            "GET", f"/v1/{self._tenant}/graph/neighbors", params=params,
        )
        return NeighborsResponse.model_validate(resp.json())

    async def expand(
        self,
        node_id: str | None = None,
        *,
        seeds: list[str] | None = None,
        max_hops: int = 2,
        direction: str = "both",
        rel_types: list[str] | None = None,
        limit: int = 100,
        communities: list[str] | None = None,
    ) -> ExpandResponse:
        """BFS expand from one or many seed nodes.

        Pass ``seeds`` for multi-seed batch expansion (the
        recommended path when starting from more than one node);
        graphos runs a single BFS over the combined frontier in one
        request -- ~N× faster than N single-seed calls.

        ``communities`` (optional): list of Louvain ``community_id``
        values rendered as strings.  When provided, graphos restricts
        the BFS frontier to vertices whose ``community_id`` is in this
        set — tight-radius traversal bounded to the seed's Louvain
        partition(s).  Empty / unset = no scope (full BFS).
        """
        if seeds is None and node_id is None:
            raise ValueError("expand requires node_id or seeds")
        # Backward compat: ``node_id`` (singular) is still required
        # at the wire level (server treats it as a fallback when
        # ``seeds`` is empty); use the first seed as the spelled-out
        # ``node_id`` so callers passing only ``seeds`` don't need to
        # pick one explicitly.
        params: dict[str, Any] = {
            "node_id": node_id or (seeds[0] if seeds else ""),
            "max_hops": max_hops,
            "direction": direction,
            "limit": limit,
        }
        if seeds:
            params["seeds"] = list(seeds)
        if rel_types:
            params["rel_types"] = rel_types
        if communities:
            params["communities"] = list(communities)
        resp = await self._request(
            "GET", f"/v1/{self._tenant}/graph/expand", params=params,
        )
        return ExpandResponse.model_validate(resp.json())

    async def induced_edges(
        self,
        node_ids: list[str],
        *,
        rel_types: list[str] | None = None,
        limit: int = 500,
    ) -> InducedEdgesResponse:
        body: dict[str, Any] = {"node_ids": node_ids, "limit": limit}
        if rel_types:
            body["rel_types"] = rel_types
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/graph/induced_edges", json_body=body,
        )
        return InducedEdgesResponse.model_validate(resp.json())

    async def scoped_subgraph(
        self,
        *,
        scope_property: str,
        scope_value: str,
        group_property: str | None = None,
        group_values: list[str] | None = None,
        include_properties: list[str] | None = None,
        exclude_property: str | None = None,
        exclude_substrings: list[str] | None = None,
        node_cap: int = 1200,
        rel_types: list[str] | None = None,
        edge_limit: int = 8000,
    ) -> dict[str, Any]:
        """POST /v1/{tenant}/graph/scoped_subgraph — returns the raw dict.

        ``{tenant, scope_property, scope_value, truncated, elapsed_ms,
        nodes[{id, label, properties}], edges[]}``.  Every property name
        is the caller's; graphos gives none of them meaning.
        """
        body: dict[str, Any] = {
            "scope_property": scope_property, "scope_value": scope_value,
            "node_cap": node_cap, "edge_limit": edge_limit,
        }
        optional = {
            "group_property": group_property, "group_values": group_values,
            "include_properties": include_properties,
            "exclude_property": exclude_property,
            "exclude_substrings": exclude_substrings, "rel_types": rel_types,
        }
        body.update({k: v for k, v in optional.items() if v})
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/graph/scoped_subgraph", json_body=body,
        )
        return resp.json()

    async def aggregate_edges(
        self,
        *,
        group_by_src: str,
        group_by_dst: str,
        rel_types: list[str] | None = None,
        src_values: list[str] | None = None,
        dst_values: list[str] | None = None,
        require_different: bool = False,
        limit: int = 200,
    ) -> EdgesAggregateResponse:
        body: dict[str, Any] = {
            "group_by_src": group_by_src,
            "group_by_dst": group_by_dst,
            "require_different": require_different,
            "limit": limit,
        }
        if rel_types:
            body["rel_types"] = rel_types
        if src_values:
            body["src_values"] = src_values
        if dst_values:
            body["dst_values"] = dst_values
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/graph/edges/aggregate", json_body=body,
        )
        return EdgesAggregateResponse.model_validate(resp.json())

    async def aggregate_nodes(
        self,
        *,
        label: str,
        group_by: str,
        values: list[str] | None = None,
        limit: int = 200,
    ) -> NodesAggregateResponse:
        """Group vertices of ``label`` by a property and count.

        Inheritance-aware: passing the parent label (e.g. ``Document``)
        matches all descendant labels via postgres table inheritance.
        Useful for live summary views (e.g. counts per ``type``) when
        no materialised snapshot exists.
        """
        body: dict[str, Any] = {
            "label": label, "group_by": group_by, "limit": limit,
        }
        if values:
            body["values"] = values
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/graph/nodes/aggregate", json_body=body,
        )
        return NodesAggregateResponse.model_validate(resp.json())

    async def sample_subgraph(
        self,
        *,
        label: str,
        filter_property: str | None = None,
        filter_value: str | None = None,
        rel_types: list[str] | None = None,
        per_rel: int = 50,
        include_properties: list[str] | None = None,
        edge_properties: list[str] | None = None,
    ) -> SampleSubgraphResponse:
        """Stratified node-and-edge sample, ``per_rel`` edges per type.

        Pure pass-through to ``POST /v1/{tenant}/graph/sample_subgraph``.
        The caller stays in control of every domain-specific knob
        (which label, which filter property, which fields to expand)
        so this method is reusable across data models.
        """
        body: dict[str, Any] = {
            "label": label,
            "per_rel": per_rel,
        }
        if filter_property is not None:
            body["filter_property"] = filter_property
        if filter_value is not None:
            body["filter_value"] = filter_value
        if rel_types:
            body["rel_types"] = rel_types
        if include_properties:
            body["include_properties"] = include_properties
        if edge_properties:
            body["edge_properties"] = edge_properties
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/graph/sample_subgraph", json_body=body,
        )
        return SampleSubgraphResponse.model_validate(resp.json())

    # ── deletes ────────────────────────────────────────────────────────────

    async def delete_node(
        self, node_id: str, *, label: str | None = None, if_match: str | None = None,
    ) -> DeleteNodeResponse:
        """Delete the node(s) with this id and their edges; only in ``label``
        when given.  With ``if_match`` (an ETag from a read), the id must name
        one node, deleted only if it still has that ETag (412 otherwise)."""
        resp = await self._request(
            "DELETE", self._node_path(node_id), params=_label_param(label),
            headers=_if_match_header(if_match),
        )
        return DeleteNodeResponse.model_validate(resp.json())

    async def delete_by_property(
        self, *, property_key: str, property_value: str,
    ) -> DeleteByPropertyResponse:
        resp = await self._request(
            "DELETE",
            f"/v1/{self._tenant}/graph/nodes",
            params={"property_key": property_key, "property_value": property_value},
        )
        return DeleteByPropertyResponse.model_validate(resp.json())

    # ── Louvain ────────────────────────────────────────────────────────────

    # ── Community reads + tenant stats ─────────────────────────────────────

    async def list_communities(
        self,
        *,
        min_size: int = 1,
        include_members: bool = False,
    ) -> CommunitiesResponse:
        params: dict[str, Any] = {"min_size": min_size, "include_members": include_members}
        resp = await self._request(
            "GET", f"/v1/{self._tenant}/graph/communities", params=params,
        )
        return CommunitiesResponse.model_validate(resp.json())

    async def community_summary(self, *, top: int = 10) -> CommunitySummary:
        resp = await self._request(
            "GET", f"/v1/{self._tenant}/graph/community-summary",
            params={"top": top},
        )
        return CommunitySummary.model_validate(resp.json())

    async def community_ids(
        self, ids: list[str],
    ) -> CommunityIdsResponse:
        """Batched ``external id → community_id`` lookup.

        One HTTP round-trip; resolves via the per-label
        ``<Label>_id_unique`` btrees.  Replaces the N+1 ``/search``-
        per-node pattern that otherwise dominates a read path's
        wall-clock budget.

        Returns a partial map: ids that graphos does not know, or
        which Louvain did not cluster, are absent from
        ``community_by_id``.  Callers should treat missing entries as
        "no community" rather than as an error.
        """
        if not ids:
            return CommunityIdsResponse(
                tenant=self._tenant,
                n_requested=0,
                n_resolved=0,
                community_by_id={},
                elapsed_ms=0,
            )
        resp = await self._request(
            "POST",
            f"/v1/{self._tenant}/graph/community-ids",
            json_body={"ids": list(ids)},
        )
        return CommunityIdsResponse.model_validate(resp.json())

    async def tenant_stats(self) -> TenantStatsResponse:
        resp = await self._request("GET", f"/v1/{self._tenant}/graph/stats")
        return TenantStatsResponse.model_validate(resp.json())

    # ── Generic analytics primitives ───────────────────────────────────────

    async def catalogue(self) -> dict[str, Any]:
        """``GET .../graph/catalogue``: property name → labels carrying it.

        Diagnostic for rule-based edges: shows which labels the resolver
        considers for each property.
        """
        resp = await self._request("GET", f"/v1/{self._tenant}/graph/catalogue")
        return resp.json()

    # ── operations (admin token) ───────────────────────────────────────────

    async def db_stats(self) -> dict[str, Any]:
        """``GET /v1/db/stats``: table sizes, dead tuples, last vacuum/analyze."""
        resp = await self._request("GET", "/v1/db/stats")
        return resp.json()

    async def slow_queries(self, limit: int = 25) -> dict[str, Any]:
        """``GET /v1/db/slow-queries``: top statements by mean time."""
        resp = await self._request("GET", "/v1/db/slow-queries", params={"limit": limit})
        return resp.json()

    async def vacuum(
        self,
        *,
        tables: list[str] | None = None,
        full: bool = False,
        analyze: bool = True,
    ) -> dict[str, Any]:
        """``POST /v1/db/vacuum``.  Without ``tables`` it vacuums the whole
        database.  Long-running, so never retried automatically."""
        resp = await self._request(
            "POST", "/v1/db/vacuum", json_body=_vacuum_body(tables, full, analyze),
            idempotent=False,
        )
        return resp.json()

    async def submit_vacuum(
        self,
        *,
        tables: list[str] | None = None,
        full: bool = False,
        analyze: bool = True,
    ) -> Job:
        """:meth:`vacuum` as a background job; see :meth:`wait_for_job`."""
        return await self._submit("/v1/db/vacuum", _vacuum_body(tables, full, analyze))

    async def label_histogram(
        self, label: str, *, property: str, limit: int = 100,
    ) -> HistogramResponse:
        """Property-value histogram for one label — one SQL GROUP BY.

        Closes the N-sequential-aggregate pattern on the caller side.
        Schema-agnostic — any registered label
        + any property name.
        """
        resp = await self._request(
            "GET",
            f"/v1/{self._tenant}/graph/labels/{label}/histogram",
            params={"property": property, "limit": limit},
        )
        return HistogramResponse.model_validate(resp.json())

    async def label_top_by_edge(
        self, label: str, *,
        by_edge: str, direction: str = "out", k: int = 20,
    ) -> TopByEdgeResponse:
        """Top-K nodes of *label* by aggregate edge-degree on *by_edge*.

        ``direction`` is ``out`` (parent is source), ``in`` (parent is
        destination), or ``both`` (UNION ALL summed).
        """
        resp = await self._request(
            "GET",
            f"/v1/{self._tenant}/graph/labels/{label}/top",
            params={"by_edge": by_edge, "direction": direction, "k": k},
        )
        return TopByEdgeResponse.model_validate(resp.json())

    def _node_path(self, external_id: str) -> str:
        return f"/v1/{self._tenant}/graph/nodes/{quote(external_id, safe='')}"

    async def get_node(
        self, external_id: str, *, label: str | None = None,
    ) -> NodeGetResponse | None:
        """Fetch one node by external id.  Returns None on 404.

        An id that exists in several labels raises ``GraphosError`` (409)
        unless ``label`` picks one.
        """
        try:
            resp = await self._request(
                "GET", self._node_path(external_id), params=_label_param(label),
            )
        except GraphosError as exc:
            if getattr(exc, "status_code", None) == HTTPStatus.NOT_FOUND:
                return None
            raise
        return NodeGetResponse.model_validate(resp.json())

    async def upsert_node_properties(
        self, external_id: str, properties: dict[str, Any], *,
        label: str | None = None, if_match: str | None = None,
    ) -> NodePropertiesUpsertResponse:
        """Merge a property bag onto the node whose ``properties.id``
        matches *external_id* (top-level keys overwrite, others are kept).
        ``label`` picks the node when the id exists in several labels;
        ``if_match`` writes only if the node still has that ETag.
        """
        resp = await self._request(
            "POST",
            f"{self._node_path(external_id)}/properties",
            params=_label_param(label),
            json_body={"properties": properties},
            headers=_if_match_header(if_match),
        )
        return NodePropertiesUpsertResponse.model_validate(resp.json())

    async def patch_node(
        self, external_id: str, patch: Mapping[str, Any], *,
        label: str | None = None, if_match: str | None = None,
    ) -> NodeGetResponse:
        """Apply a JSON Merge Patch (RFC 7396) to one node's properties.

        ``None`` removes a key, a nested dict merges key by key, any other
        value replaces; keys not in ``patch`` are kept.  With ``if_match``
        (the ``etag`` of a read) the patch is applied only if the node has
        not changed since; otherwise ``GraphosError`` 412.  Returns the node
        as written, with its new ``etag``.
        """
        resp = await self._request(
            "PATCH",
            self._node_path(external_id),
            params=_label_param(label),
            content=json.dumps(dict(patch)),
            content_type=_MERGE_PATCH,
            headers=_if_match_header(if_match),
        )
        return NodeGetResponse.model_validate(resp.json())

    # ── One edge, by identity ──────────────────────────────────────────────
    #
    # An edge is named by its type and its end ids; ``src_label`` /
    # ``dst_label`` pick one when an end id exists in several labels (the
    # server answers 409 until they do).

    def _edge_request_args(
        self, edge_type: str, src_id: str, dst_id: str,
        src_label: str | None, dst_label: str | None,
    ) -> tuple[str, dict[str, str]]:
        params = {"src": src_id, "dst": dst_id}
        if src_label is not None:
            params["src_label"] = src_label
        if dst_label is not None:
            params["dst_label"] = dst_label
        return f"/v1/{self._tenant}/graph/edges/{quote(edge_type, safe='')}", params

    async def get_edge(
        self, edge_type: str, src_id: str, dst_id: str, *,
        src_label: str | None = None, dst_label: str | None = None,
    ) -> EdgeGetResponse | None:
        """Fetch one edge.  Returns None on 404."""
        path, params = self._edge_request_args(edge_type, src_id, dst_id, src_label, dst_label)
        try:
            resp = await self._request("GET", path, params=params)
        except GraphosError as exc:
            if getattr(exc, "status_code", None) == HTTPStatus.NOT_FOUND:
                return None
            raise
        return EdgeGetResponse.model_validate(resp.json())

    async def patch_edge(  # noqa: PLR0913 — the edge's identity has this many parts
        self, edge_type: str, src_id: str, dst_id: str, patch: Mapping[str, Any], *,
        src_label: str | None = None, dst_label: str | None = None,
        if_match: str | None = None,
    ) -> EdgeGetResponse:
        """Apply a JSON Merge Patch (RFC 7396) to one edge's properties;
        with ``if_match``, only if the edge still has that ETag."""
        path, params = self._edge_request_args(edge_type, src_id, dst_id, src_label, dst_label)
        resp = await self._request(
            "PATCH", path, params=params,
            content=json.dumps(dict(patch)), content_type=_MERGE_PATCH,
            headers=_if_match_header(if_match),
        )
        return EdgeGetResponse.model_validate(resp.json())

    async def delete_edge(  # noqa: PLR0913 — the edge's identity has this many parts
        self, edge_type: str, src_id: str, dst_id: str, *,
        src_label: str | None = None, dst_label: str | None = None,
        if_match: str | None = None,
    ) -> DeleteEdgeResponse:
        """Delete one edge; its end nodes stay.  Idempotent; with
        ``if_match``, only if the edge still has that ETag."""
        path, params = self._edge_request_args(edge_type, src_id, dst_id, src_label, dst_label)
        resp = await self._request(
            "DELETE", path, params=params, headers=_if_match_header(if_match),
        )
        return DeleteEdgeResponse.model_validate(resp.json())

    # ── Shortest paths ─────────────────────────────────────────────────────

    async def find_paths(  # noqa: PLR0913 — the search's options
        self, src: str, dst: str, *,
        src_label: str | None = None, dst_label: str | None = None,
        rel_types: Iterable[str] | None = None, direction: str = "both",
        max_hops: int = 4, limit: int = 1, include_properties: bool = False,
    ) -> PathsResponse:
        """The shortest paths from node ``src`` to node ``dst``.

        ``direction``: ``out`` follows edges the way they point, ``in``
        against it, ``both`` either way.  Up to ``limit`` paths, all of the
        shortest length within ``max_hops``; ``hops`` is None when there is
        none, and ``truncated`` when the search hit its edge cap.
        """
        body: dict[str, Any] = {
            "src": src, "dst": dst, "direction": direction,
            "max_hops": max_hops, "limit": limit, "include_properties": include_properties,
        }
        if src_label is not None:
            body["src_label"] = src_label
        if dst_label is not None:
            body["dst_label"] = dst_label
        if rel_types is not None:
            body["rel_types"] = list(rel_types)
        resp = await self._request("POST", f"/v1/{self._tenant}/graph/paths", json_body=body)
        return PathsResponse.model_validate(resp.json())

    # ── Schema-agnostic K/V cache ──────────────────────────────────────────

    async def cache_put(
        self, key: str, value: Any, *, ttl_seconds: int | None = None,
    ) -> CachePutResponse:
        """Upsert a JSON value into the per-tenant K/V cache."""
        body: dict[str, Any] = {"value": value}
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        resp = await self._request(
            "PUT",
            f"/v1/{self._tenant}/graph/cache/{key}",
            json_body=body,
        )
        return CachePutResponse.model_validate(resp.json())

    async def cache_get(self, key: str) -> CacheGetResponse | None:
        """Fetch a cached value; returns ``None`` on 404."""
        try:
            resp = await self._request(
                "GET", f"/v1/{self._tenant}/graph/cache/{key}",
            )
        except GraphosError as exc:
            if getattr(exc, "status_code", None) == HTTPStatus.NOT_FOUND:
                return None
            raise
        return CacheGetResponse.model_validate(resp.json())

    async def cache_delete(self, key: str) -> None:
        """Invalidate a cached value (LRU + PG)."""
        await self._request(
            "DELETE", f"/v1/{self._tenant}/graph/cache/{key}",
        )

    # ── Export ─────────────────────────────────────────────────────────────

    async def export_graph(
        self,
        *,
        node_ids: list[str] | None = None,
        rel_types: list[str] | None = None,
        limit_nodes: int = 5000,
    ) -> GraphologyExport:
        """Export a subgraph in graphology's native serialization format.

        Consumable by sigma.js via ``Graph.from(payload)`` — no extra
        client-side parser needed.
        """
        params: dict[str, Any] = {"limit_nodes": limit_nodes}
        if node_ids:
            params["node_ids"] = node_ids
        if rel_types:
            params["rel_types"] = rel_types
        resp = await self._request(
            "GET", f"/v1/{self._tenant}/graph/export", params=params,
        )
        return GraphologyExport.model_validate(resp.json())

    # ── Louvain recompute ─────────────────────────────────────────────────

    async def recompute_communities(
        self,
        *,
        rel_types: list[str] | None = None,
        rel_weights: Mapping[str, float] | None = None,
        node_filter: Mapping[str, Any] | None = None,
        resolution: float = 1.0,
        min_community_size: int = 3,
        dry_run: bool = False,
    ) -> RecomputeCommunitiesResponse:
        """Recompute Louvain communities over the tenant subgraph.

        ``node_filter`` scopes the recompute by property predicate — each
        value is either a scalar (``=``) or a list (``IN``). Both endpoints
        of every traversed edge must match. A per-project
        recompute is ``node_filter={"project_id": "<sid>"}``.
        """
        body = _recompute_body(
            rel_types=rel_types, rel_weights=rel_weights, node_filter=node_filter,
            resolution=resolution, min_community_size=min_community_size, dry_run=dry_run,
        )
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/recompute-communities", json_body=body,
            idempotent=False,   # long-running: never re-run on a 5xx
        )
        return RecomputeCommunitiesResponse.model_validate(resp.json())

    async def submit_recompute_communities(
        self,
        *,
        rel_types: list[str] | None = None,
        rel_weights: Mapping[str, float] | None = None,
        node_filter: Mapping[str, Any] | None = None,
        resolution: float = 1.0,
        min_community_size: int = 3,
        dry_run: bool = False,
    ) -> Job:
        """:meth:`recompute_communities` as a background job.  On success the
        job's ``result`` is the :class:`RecomputeCommunitiesResponse` body.
        A second submission while one is queued or running raises 409."""
        body = _recompute_body(
            rel_types=rel_types, rel_weights=rel_weights, node_filter=node_filter,
            resolution=resolution, min_community_size=min_community_size, dry_run=dry_run,
        )
        return await self._submit(f"/v1/{self._tenant}/recompute-communities", body)

    # ── Background jobs ───────────────────────────────────────────────────

    async def _submit(self, path: str, body: dict[str, Any]) -> Job:
        # Never retried: a retry after a lost response would queue it twice.
        resp = await self._request(
            "POST", path, json_body=body, headers={"Prefer": "respond-async"},
            idempotent=False,
        )
        return Job.model_validate(resp.json())

    async def get_job(self, job_id: UUID | str) -> Job:
        """``GET /v1/jobs/{id}``."""
        resp = await self._request("GET", f"/v1/jobs/{job_id}")
        return Job.model_validate(resp.json())

    async def list_jobs(
        self,
        *,
        tenant: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Job]:
        """``GET /v1/jobs``: newest first, only jobs this token may see."""
        params: dict[str, Any] = {"limit": limit}
        if tenant is not None:
            params["tenant"] = tenant
        if status is not None:
            params["status"] = status
        resp = await self._request("GET", "/v1/jobs", params=params)
        return [Job.model_validate(j) for j in resp.json()["jobs"]]

    async def cancel_job(self, job_id: UUID | str) -> Job:
        """``POST /v1/jobs/{id}:cancel``.  A queued job is cancelled at once;
        a running one is stopped (poll for the final status).  409 if it has
        already finished."""
        resp = await self._request("POST", f"/v1/jobs/{job_id}:cancel", idempotent=False)
        return Job.model_validate(resp.json())

    async def wait_for_job(
        self,
        job_id: UUID | str,
        *,
        poll_s: float = 2.0,
        timeout_s: float | None = None,
    ) -> Job:
        """Poll until the job has finished and return it, whatever the
        outcome; check ``job.status`` (and ``job.error``).  Raises
        ``TimeoutError`` after ``timeout_s``."""
        async with asyncio.timeout(timeout_s):
            while True:
                job = await self.get_job(job_id)
                if job.done:
                    return job
                await asyncio.sleep(poll_s)


def _resolve_body(edge_types: Iterable[str] | None) -> dict[str, Any]:
    return {} if edge_types is None else {"edge_types": list(edge_types)}


def _vacuum_body(tables: list[str] | None, full: bool, analyze: bool) -> dict[str, Any]:
    body: dict[str, Any] = {"full": full, "analyze": analyze}
    if tables:
        body["tables"] = list(tables)
    return body


def _recompute_body(
    *,
    rel_types: list[str] | None,
    rel_weights: Mapping[str, float] | None,
    node_filter: Mapping[str, Any] | None,
    resolution: float,
    min_community_size: int,
    dry_run: bool,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "resolution": resolution,
        "min_community_size": min_community_size,
        "dry_run": dry_run,
    }
    if rel_types:
        body["rel_types"] = rel_types
    if rel_weights:
        body["rel_weights"] = dict(rel_weights)
    if node_filter:
        body["node_filter"] = dict(node_filter)
    return body


def _extract_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return resp.text[:200]

# bust-cache: 1779959546

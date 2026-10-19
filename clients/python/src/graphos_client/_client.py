"""Async HTTP client for graphos."""

from __future__ import annotations

import asyncio
import gzip as gzip_module
import json
import logging
import os
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from http import HTTPStatus
from pathlib import Path
from typing import IO, Any, Literal
from urllib.parse import quote
from uuid import UUID

import httpx

from graphos_client._auth import PasswordAuth, from_environment
from graphos_client._errors import GraphosError, GraphosNotReady
from graphos_client._models import (
    BulkEdgesResponse,
    BulkNodesResponse,
    CacheGetResponse,
    CachePutResponse,
    ChangeItem,
    ChangesHistory,
    ChangesPage,
    CommunitiesResponse,
    CommunityIdsResponse,
    CommunityMap,
    CommunitySummary,
    ComputeMetricsResponse,
    CsvImportResponse,
    CypherResult,
    DeleteByPropertyResponse,
    DeleteEdgeResponse,
    DeleteNodeResponse,
    DeleteNodesResponse,
    EdgeCheckItem,
    EdgeGetResponse,
    EdgesAggregateResponse,
    EdgesCheckResponse,
    EdgesReconcileResponse,
    EdgesScanResponse,
    EdgesUpsertResponse,
    EdgeUpsert,
    ExpandResponse,
    GraphMap,
    GraphologyExport,
    HubsResponse,
    InducedEdgesResponse,
    Job,
    Me,
    NeighborsResponse,
    NodeGetResponse,
    NodePropertiesUpsertResponse,
    NodeRef,
    NodesAggregateResponse,
    NodesScanResponse,
    NodesUpsertResponse,
    NodeUpsert,
    PathsResponse,
    PropertiesCheckResponse,
    PropertyDefinitionsResponse,
    RecomputeCommunitiesResponse,
    ReconcileEdge,
    SampleSubgraphResponse,
    SchemaSummaryResponse,
    SearchResponse,
    TenantInfo,
    TenantSchemaInfo,
    TenantStatsResponse,
    TopByEdgeResponse,
    UnregisteredTenant,
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


def read_dataset_header(source: str | os.PathLike[str] | IO[bytes]) -> dict[str, Any] | None:
    """The header line of a dataset file (gzip or not), or ``None`` without one.

    A file object is read from where it is and put back there.
    """
    with _opened(source, "rb") as raw:
        start = raw.tell() if raw.seekable() else None
        magic = raw.read(2)
        raw.seek(start or 0)
        stream: IO[bytes] = gzip_module.GzipFile(fileobj=raw) if magic == b"\x1f\x8b" else raw
        first = stream.readline()
        if start is not None:
            raw.seek(start)
    try:
        line = json.loads(first)
    except ValueError:
        return None
    return line if isinstance(line, dict) and line.get("_kind") == "header" else None


class _opened:  # noqa: N801 — used like ``open``
    """``open(path)`` for a path; a file object passes through, left open."""

    def __init__(self, target: str | os.PathLike[str] | IO[bytes], mode: str) -> None:
        self._target, self._mode, self._file = target, mode, None

    def __enter__(self) -> IO[bytes]:
        if isinstance(self._target, (str, os.PathLike)):
            self._file = Path(self._target).open(self._mode)  # noqa: SIM115
            return self._file
        return self._target

    def __exit__(self, *exc: object) -> None:
        if self._file is not None:
            self._file.close()


# Declared label pairs one registration may carry (the server's limit).
_MAX_PAIRS_PER_CALL = 5000


def _by_type(pairs: list[tuple[str, Any]]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for edge_type, pair in pairs:
        out.setdefault(edge_type, []).append(pair)
    return out


class GraphosClient:
    """Async HTTP client for the graphos service.

    Use as a context manager so the underlying httpx pool gets closed::

        async with GraphosClient(base_url=..., tenant=...,
                                 auth=PasswordAuth("svc", secret)) as cli:
            await cli.search(...)

    Or hold the client across many calls and ``close()`` explicitly.
    """

    def __init__(
        self,
        *,
        base_url: str,
        tenant: str,
        token: str | None = None,
        auth: httpx.Auth | None = None,
        timeout_s: float = 30.0,
        retries: int = 3,
        retry_backoff_s: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Construct the client.

        How it signs in, first match wins:

        - ``auth``: usually :class:`PasswordAuth` (a user or service account
          and its password), which signs in and renews the short-lived
          access token by itself;
        - ``token``: a bearer token used as is (one obtained elsewhere; it
          is not renewed);
        - ``GRAPHOS_USER`` and ``GRAPHOS_PASSWORD`` in the environment, as
          ``PasswordAuth``;
        - none: no Authorization header, for a graphos running with
          ``GRAPHOS_AUTH_DISABLED=true`` (local development).
        """
        if token and auth is not None:
            raise ValueError("pass either token or auth, not both")
        self._base_url = base_url.rstrip("/")
        self._token = token or None
        if self._token is None and auth is None:
            auth = from_environment()
        if isinstance(auth, PasswordAuth):
            auth.bind(self._base_url)
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
            auth=auth,
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

    async def list_unregistered_tenants(self) -> list[UnregisteredTenant]:
        """Tenants unregistered with their data kept that the caller owns
        (every one, orphan graphs included, for an admin)."""
        resp = await self._request("GET", "/v1/tenants", params={"unregistered": "true"})
        return [UnregisteredTenant.model_validate(t) for t in resp.json().get("unregistered", [])]

    async def me(self) -> Me:
        """``GET /v1/me``: who is signed in, their level per tenant and rights."""
        resp = await self._request("GET", "/v1/me")
        return Me.model_validate(resp.json())

    async def schema(self, name: str | None = None) -> TenantSchemaInfo:
        """``GET /v1/{tenant}/schema``: labels, hierarchy, declared endpoints and
        search indexes (any token)."""
        resp = await self._request("GET", f"/v1/{name or self._tenant}/schema")
        return TenantSchemaInfo.model_validate(resp.json())

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
        property_indexes: Iterable[Mapping[str, Any]] = (),
        generated_columns: Iterable[Mapping[str, Any]] = (),
        label_hierarchy: Mapping[str, str] | None = None,
        meta: Mapping[str, str | None] | None = None,
        search_indexes: Mapping[str, Mapping[str, Any]] | None = None,
        edge_endpoints: Mapping[str, Iterable[tuple[str, str] | Mapping[str, str]]] | None = None,
        strict_endpoints: bool | None = None,
        display: Mapping[str, Mapping[str, Mapping[str, Any] | None]] | None = None,
        property_schema: Mapping[str, Any] | None = None,
        force_shrink: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Register / re-register a tenant.

        What is declared is added to what is registered; ``force_shrink=True``
        replaces the stored schema instead (labels left out keep their tables
        and data).  ``dry_run=True`` registers nothing and answers what the call
        would change (``changes``, as the change feed would record it), what
        applying it would cost (``warnings``) and what would stop working
        (``breaking``: removed labels, indexes or rules, tighter strict rules).

        ``meta`` holds free-form notes about the tenant; keys are added or
        overwritten, and a key given as ``None`` is removed.

        ``edge_endpoints`` declares, per edge type, the label pairs it may join:
        ``{"CALLS": [("Function", "Function")], "DEFINES": [("File", "Symbol")]}``
        (pairs or ``{"src": ..., "dst": ...}``).  They are added to what is
        declared; a child label satisfies a pair declared for its parent.
        ``strict_endpoints=True`` makes graphos refuse writes of a declared type
        outside its pairs; ``None`` leaves the setting as it is.  See
        :meth:`remove_edge_endpoint`.

        ``property_indexes`` declares indexes on vertex-label properties, which
        serve ``where`` filters: ``[{"label": "Document", "properties": ["kind"]}]``,
        with ``"unique": True`` to refuse duplicates (409) or ``"kind": "trigram"``
        for substring search on one property.  ``generated_columns`` declares
        stored columns for direct SQL readers:
        ``[{"label": "Document", "column": "kind", "property": "kind", "type": "text"}]``
        (``text``, ``numeric``, ``boolean`` or ``jsonb``).  graphos writes the SQL
        for both; both are added to what is registered.

        ``search_indexes`` declares named search indexes for :meth:`search`:
        ``{"names": {"labels": ["*"], "properties": ["name"], "analyzer": "identifier"}}``;
        ``analyzer`` is ``identifier`` (names, ids, paths) or ``text`` (prose,
        with ``language``).  Declaring a name again replaces its definition.

        ``display`` says how the web UI draws labels and edge types, for everyone
        on the tenant: ``{"vertex": {"Person": {"color": "#5FA584", "caption":
        "full_name", "size_by": "graphos_pagerank", "icon": "person"}}, "edge":
        {"AUTHORED": {"color": "#8D97AE", "caption": "role"}}}``.  An entry given
        again replaces the stored one; ``None`` removes it.

        ``property_schema`` declares the properties each label and edge type
        carries and how writes are held to them: ``{"mode": "warn", "vertex":
        {"*": {"properties": {"source": {"type": "string", "required": True}}},
        "Person": {"description": "...", "properties": {"email": {"type":
        "string", "pattern": "[^@]+@[^@]+"}}}}, "edge": {...}}``. ``mode`` is
        ``off`` (stored, nothing checked), ``warn`` or ``strict``; an entry given
        again replaces the stored one, ``None`` removes it.

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
            "property_indexes": [dict(x) for x in property_indexes],
            "generated_columns": [dict(x) for x in generated_columns],
            "label_hierarchy": dict(label_hierarchy or {}),
            "meta": dict(meta or {}),
        }
        if search_indexes:
            body["search_indexes"] = {k: dict(v) for k, v in search_indexes.items()}
        if edge_endpoints:
            body["edge_endpoints"] = {
                edge_type: [p if isinstance(p, Mapping) else list(p) for p in pairs]
                for edge_type, pairs in edge_endpoints.items()
            }
        if strict_endpoints is not None:
            body["strict_endpoints"] = strict_endpoints
        if display:
            body["display"] = {part: dict(entries) for part, entries in display.items()}
        if property_schema:
            body["property_schema"] = dict(property_schema)
        return await self._register(body, force_shrink=force_shrink, dry_run=dry_run)

    async def _register(
        self, body: Mapping[str, Any], *, force_shrink: bool, dry_run: bool,
    ) -> dict[str, Any]:
        flags = (("force_shrink", force_shrink), ("dry_run", dry_run))
        params = {k: "true" for k, on in flags if on}
        resp = await self._request("POST", "/v1/tenants", params=params or None, json_body=body)
        return resp.json()

    async def export_schema(self, name: str | None = None) -> dict[str, Any]:
        """``GET /v1/tenants/{name}/spec``: the tenant's whole schema as a document
        (``graphos.schema``) that :meth:`import_schema` reads back (``maintain``)."""
        resp = await self._request("GET", f"/v1/tenants/{name or self._tenant}/spec")
        return resp.json()

    async def import_schema(
        self,
        document: Mapping[str, Any],
        *,
        name: str | None = None,
        replace: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Register the schema in ``document`` (from :meth:`export_schema`).

        ``name`` imports it under another tenant name (a new tenant needs
        ``create_tenants``); ``replace`` replaces the stored schema instead of
        adding to it; ``dry_run`` answers what it would change, writing nothing.

        A registration takes at most 5,000 declared label pairs; a document with
        more is sent in several calls, the first with everything else (and
        ``replace``), the rest adding pairs.  A dry run previews the first 5,000
        and says how many it left out.
        """
        body = {k: v for k, v in document.items() if k != "edge_endpoints"}
        if name is not None:
            body["name"] = name
        target = str(body.get("name") or self._tenant)
        body["name"] = target
        pairs = [
            (edge_type, pair)
            for edge_type, type_pairs in (document.get("edge_endpoints") or {}).items()
            for pair in type_pairs
        ]
        chunks = [pairs[i:i + _MAX_PAIRS_PER_CALL]
                  for i in range(0, len(pairs), _MAX_PAIRS_PER_CALL)] or [[]]
        if "edge_endpoints" in document:
            body["edge_endpoints"] = _by_type(chunks[0])
        result = await self._register(body, force_shrink=replace, dry_run=dry_run)
        if dry_run:
            rest = len(pairs) - len(chunks[0])
            if rest:
                result.setdefault("warnings", []).append(
                    f"{rest} more declared label pairs are not in this preview "
                    "(at most 5000 per call); the import adds them"
                )
            return result
        for chunk in chunks[1:]:
            result = await self._register(
                {"name": target, "edge_endpoints": _by_type(chunk)},
                force_shrink=False, dry_run=False,
            )
        return result

    async def remove_edge_endpoint(
        self, edge_type: str, src_label: str, dst_label: str, *, name: str | None = None,
    ) -> dict[str, Any]:
        """Remove one declared label pair (maintain).  No data is touched; a type
        with no pairs left accepts any pair again."""
        target = name or self._tenant
        resp = await self._request(
            "DELETE", f"/v1/tenants/{target}/edge_endpoints",
            params={"type": edge_type, "src": src_label, "dst": dst_label},
        )
        return resp.json()

    async def unregister_tenant(
        self, name: str | None = None, *, drop_data: bool = False,
    ) -> dict[str, Any]:
        """Unregister a tenant (owner).  Its data, schema and grants are kept and
        :meth:`restore_tenant` brings it back.  ``drop_data=True`` deletes it for
        good instead, registered or unregistered: graph, data, registration,
        cache, change records.  That cannot be undone."""
        target = name or self._tenant
        params = {"drop_data": "true"} if drop_data else None
        resp = await self._request(
            "DELETE", f"/v1/tenants/{target}", params=params, idempotent=not drop_data,
        )
        return resp.json()

    async def cypher(
        self,
        query: str,
        params: Mapping[str, Any] | None = None,
        *,
        limit: int | None = None,
    ) -> CypherResult:
        """Run a read-only Cypher query on this tenant's graph (the Cypher console,
        when the server turns it on; 404 otherwise).  Parameters are used in the
        query as ``$name``.  Not retried: a slow query is not run twice."""
        body: dict[str, Any] = {"query": query, "params": dict(params or {})}
        if limit is not None:
            body["limit"] = limit
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/cypher", json_body=body, idempotent=False,
        )
        return CypherResult.model_validate(resp.json())

    async def saved_queries(self) -> list[dict[str, Any]]:
        """The tenant's saved Cypher queries."""
        resp = await self._request("GET", f"/v1/{self._tenant}/cypher/saved")
        return list(resp.json())

    async def saved_query(self, name: str) -> dict[str, Any] | None:
        """One saved Cypher query by name (its text, description, parameters);
        None when there is none."""
        try:
            resp = await self._request(
                "GET", f"/v1/{self._tenant}/cypher/saved/{quote(name, safe='')}",
            )
        except GraphosError as exc:
            if getattr(exc, "status_code", None) == HTTPStatus.NOT_FOUND:
                return None
            raise
        return dict(resp.json())

    async def save_query(  # noqa: PLR0913 — a saved query's fields
        self,
        name: str,
        query: str,
        *,
        description: str = "",
        params: Mapping[str, Mapping[str, Any]] | None = None,
        show: str = "table",
    ) -> dict[str, Any]:
        """Save a Cypher query under ``name`` (``maintain``); ``params`` maps each
        parameter to ``{"type": "string" | "number" | "boolean" | "list", "label": ...}``."""
        body = {
            "description": description,
            "query": query,
            "params": {k: dict(v) for k, v in (params or {}).items()},
            "show": show,
        }
        resp = await self._request(
            "PUT", f"/v1/{self._tenant}/cypher/saved/{name}", json_body=body,
        )
        return resp.json()

    async def delete_saved_query(self, name: str) -> None:
        """Delete a saved Cypher query (``maintain``)."""
        await self._request("DELETE", f"/v1/{self._tenant}/cypher/saved/{name}")

    async def run_saved_query(
        self, name: str, params: Mapping[str, Any] | None = None, *, limit: int | None = None,
    ) -> CypherResult:
        """Run a saved Cypher query with its parameters."""
        body: dict[str, Any] = {"params": dict(params or {})}
        if limit is not None:
            body["limit"] = limit
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/cypher/saved/{name}:run",
            json_body=body, idempotent=False,
        )
        return CypherResult.model_validate(resp.json())

    async def restore_tenant(self, name: str | None = None) -> dict[str, Any]:
        """Register an unregistered tenant again, as it was (owner)."""
        target = name or self._tenant
        resp = await self._request(
            "POST", f"/v1/tenants/{target}/restore", idempotent=False,
        )
        return resp.json()

    async def resolve_edges(
        self,
        *,
        edge_types: Iterable[str] | None = None,
        mode: Literal["insert", "reconcile"] = "insert",
        adopt_unmarked: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """``POST /v1/{tenant}/graph/edges:resolve``.

        Trigger server-side cross-property edge derivation for every
        edge label registered with a ``resolve_rule``.  See
        ``graphos.contracts.tenants.ResolveEdgesRequest`` for the body
        shape and ``ResolveEdgesResponse`` for the result.

        ``edge_types`` selects a subset of registered types (each must
        already have a rule on the tenant spec); omit to resolve all
        registered types.

        ``mode="reconcile"`` also deletes the edges a rule derived (marked
        ``graphos_derived``) that no rule derives any more; ``adopt_unmarked``
        takes unmarked edges between the rules' label pairs as derived;
        ``dry_run`` answers the counts and writes nothing.

        Synchronous — each rule compiles to one parameterised INSERT,
        wall-clock budget is seconds even on large tenants.  Idempotent
        via ``ON CONFLICT (start_id, end_id) DO NOTHING``.
        """
        resp = await self._request(
            "POST",
            f"/v1/{self._tenant}/graph/edges:resolve",
            json_body=_resolve_body(edge_types, mode, adopt_unmarked, dry_run),
            idempotent=False,   # long-running: never re-run on a 5xx
        )
        return resp.json()

    async def submit_resolve_edges(
        self,
        *,
        edge_types: Iterable[str] | None = None,
        mode: Literal["insert", "reconcile"] = "insert",
        adopt_unmarked: bool = False,
        dry_run: bool = False,
    ) -> Job:
        """:meth:`resolve_edges` as a background job; see :meth:`wait_for_job`."""
        return await self._submit(
            f"/v1/{self._tenant}/graph/edges:resolve",
            _resolve_body(edge_types, mode, adopt_unmarked, dry_run),
        )

    async def reconcile_node_edges(  # noqa: PLR0913 — the scope has this many parts
        self,
        *,
        label: str,
        node_id: str,
        edge_type: str,
        other_label: str,
        targets: Iterable[str | tuple[str, Mapping[str, Any] | None]],
        direction: Literal["out", "in"] = "out",
        props: Literal["merge", "replace"] = "merge",
        dry_run: bool = False,
    ) -> EdgesReconcileResponse:
        """Make the ``edge_type`` edges of one node towards ``other_label``
        exactly ``targets`` (ids of the other end, or ``(id, properties)``), in
        one transaction: missing ones added, the rest removed (ADR-0001)."""
        edges = [
            {"id": t} if isinstance(t, str) else {"id": t[0], "props": t[1]} for t in targets
        ]
        body = {
            "scope": "node", "type": edge_type, "node": {"label": label, "id": node_id},
            "direction": direction, "other_label": other_label, "edges": edges, "props": props,
        }
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/graph/edges:reconcile", json_body=body,
            params={"dry_run": "true"} if dry_run else None,
        )
        return EdgesReconcileResponse.model_validate(resp.json())

    async def reconcile_type_edges(  # noqa: PLR0913 — the scope has this many parts
        self,
        *,
        edge_type: str,
        src_label: str,
        dst_label: str,
        edges: Iterable[ReconcileEdge],
        props: Literal["merge", "replace"] = "merge",
        dry_run: bool = False,
    ) -> EdgesReconcileResponse:
        """Make every ``edge_type`` edge from ``src_label`` to ``dst_label``
        exactly ``edges``, in one transaction (sent as NDJSON, so the set can be
        as large as the type).  Safe to retry: the same set gives the same
        result."""
        header = {"scope": "type", "type": edge_type, "src_label": src_label,
                  "dst_label": dst_label, "props": props}
        lines = [json.dumps(header), *(json.dumps(e.model_dump(exclude_none=True)) for e in edges)]
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/graph/edges:reconcile",
            content=("\n".join(lines) + "\n").encode("utf-8"),
            content_type="application/x-ndjson",
            params={"dry_run": "true"} if dry_run else None,
            # A whole type's edges: long, and not resent on a 5xx.
            idempotent=False,
        )
        return EdgesReconcileResponse.model_validate(resp.json())

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

    async def import_csv_nodes(  # noqa: PLR0913 — the mapping has this many parts
        self,
        data: bytes | str | os.PathLike[str],
        *,
        label: str,
        id_column: str | None = None,
        columns: Mapping[str, Mapping[str, Any]] | None = None,
        delimiter: str | None = None,
        dry_run: bool = False,
    ) -> CsvImportResponse:
        """Import a CSV file as nodes of ``label`` (``POST …/graph/nodes:csv``).

        ``data`` is the file's bytes, its text, or a path to it.  graphos
        reads it: the id column (``id_column``, else one named ``id`` or
        ``key``, else the first), every other column a property named after
        its header unless ``columns`` maps it (header → ``{"property": name or
        None, "type": "auto" | "text" | "number" | "boolean"}``).  Rows
        repeating an id are merged.  ``dry_run`` checks every row and writes
        nothing.
        """
        params = {"label": label, "id": id_column}
        return await self._import_csv(
            "nodes:csv", data, params=params, columns=columns, delimiter=delimiter, dry_run=dry_run,
        )

    async def import_csv_edges(  # noqa: PLR0913 — the mapping has this many parts
        self,
        data: bytes | str | os.PathLike[str],
        *,
        edge_type: str,
        src_label: str,
        dst_label: str,
        src_column: str | None = None,
        dst_column: str | None = None,
        columns: Mapping[str, Mapping[str, Any]] | None = None,
        delimiter: str | None = None,
        dry_run: bool = False,
    ) -> CsvImportResponse:
        """Import a CSV file as relationships of ``edge_type`` from
        ``src_label`` to ``dst_label`` nodes (``POST …/graph/edges:csv``), as
        :meth:`import_csv_nodes` does nodes.  The end columns default to ones
        named ``src`` / ``source`` / ``from`` and ``dst`` / ``target`` / ``to``.
        A relationship whose end does not exist is counted in
        ``edges_skipped``.
        """
        params = {
            "type": edge_type, "src_label": src_label, "dst_label": dst_label,
            "src": src_column, "dst": dst_column,
        }
        return await self._import_csv(
            "edges:csv", data, params=params, columns=columns, delimiter=delimiter, dry_run=dry_run,
        )

    async def _import_csv(  # noqa: PLR0913 — one call's parts
        self,
        endpoint: str,
        data: bytes | str | os.PathLike[str],
        *,
        params: dict[str, Any],
        columns: Mapping[str, Mapping[str, Any]] | None,
        delimiter: str | None,
        dry_run: bool,
    ) -> CsvImportResponse:
        if isinstance(data, os.PathLike):
            body = Path(data).read_bytes()
        else:
            body = data.encode("utf-8") if isinstance(data, str) else data
        query = {
            **{k: v for k, v in params.items() if v is not None},
            "dry_run": str(dry_run).lower(),
        }
        if columns is not None:
            query["columns"] = json.dumps({k: dict(v) for k, v in columns.items()})
        if delimiter is not None:
            query["delimiter"] = delimiter
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/graph/{endpoint}",
            params=query, content=body, content_type="text/csv",
        )
        return CsvImportResponse.model_validate(resp.json())

    async def snapshot_import(
        self,
        nodes: Iterable[NodeUpsert] = (),
        edges: Iterable[EdgeUpsert] = (),
        *,
        mode: str = "merge",
        properties: str = "enforce",
    ) -> dict[str, Any]:
        """``POST .../graph/snapshot-import``: nodes and edges in one stream.

        ``mode="empty"`` is refused (409) unless the tenant has no nodes.
        ``properties="report"`` checks the rows against the property schema and
        counts what they break without refusing any, to restore older data.

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
            params={"mode": mode, "properties": properties},
            content="\n".join(lines).encode("utf-8"),
            content_type="application/x-ndjson",
            idempotent=False,
        )
        return resp.json()

    async def export_dataset(
        self,
        destination: str | os.PathLike[str] | IO[bytes],
        *,
        labels: Sequence[str] | None = None,
        types: Sequence[str] | None = None,
        computed: bool = True,
        gzip: bool | None = None,
    ) -> dict[str, Any]:
        """``GET .../graph/snapshot``: the tenant's data, streamed to a file.

        ``destination`` is a path or a binary file object.  The file is the
        NDJSON :meth:`import_dataset` reads: a header line (format, counts, the
        schema), every node, every edge, and an end line.  ``gzip`` defaults to
        whether the path ends in ``.gz``; ``labels`` / ``types`` narrow it;
        ``computed=False`` leaves out ``graphos_*`` and ``community_id``.
        Memory stays flat.  Returns ``{"bytes": …}``.
        """
        if gzip is None:
            gzip = isinstance(destination, (str, os.PathLike)) and str(destination).endswith(".gz")
        params: dict[str, Any] = {"computed": str(computed).lower(), "gzip": str(gzip).lower()}
        if labels is not None:
            params["labels"] = list(labels)
        if types is not None:
            params["types"] = list(types)
        url = f"/v1/{self._tenant}/graph/snapshot"
        written = 0
        with _opened(destination, "wb") as out:
            async with self._http.stream("GET", url, params=params, timeout=None) as resp:
                if resp.status_code >= httpx.codes.BAD_REQUEST:
                    await resp.aread()
                    raise GraphosError(resp.status_code, _extract_detail(resp), "GET", url)
                async for chunk in resp.aiter_bytes():
                    out.write(chunk)
                    written += len(chunk)
        return {"bytes": written}

    async def import_dataset(
        self,
        source: str | os.PathLike[str] | IO[bytes],
        *,
        mode: str = "merge",
        schema: str = "check",
        properties: str = "enforce",
        name: str | None = None,
    ) -> dict[str, Any]:
        """``POST .../graph/snapshot-import``: a file from :meth:`export_dataset`.

        Streamed from ``source`` (a path or a binary file object; gzip or
        not), never held in memory.  ``mode="empty"`` restores into a tenant
        without nodes (409 otherwise); ``"merge"`` adds to what is there.
        ``schema="apply"`` first imports the schema from the file's header
        (:meth:`import_schema`, creating the tenant when new), so one file
        restores a tenant from nothing; ``"check"`` expects the labels to be
        registered.  ``properties="report"`` counts the rows' property problems
        without refusing any.  ``name`` imports into another tenant than this
        client's.

        Returns the import's result: counts, ``complete`` (false when the file
        was cut off before its end line), ``errors``.  Not retried.
        """
        target = name or self._tenant
        if schema == "apply":
            header = read_dataset_header(source)
            if header is None:
                raise ValueError("schema='apply' needs a file with a header line")
            await self.import_schema(header["schema"], name=target)
        # Renew the token now: a streamed body cannot be sent again after a 401.
        await self.me()
        url = f"/v1/{target}/graph/snapshot-import"

        async def body() -> AsyncIterator[bytes]:
            with _opened(source, "rb") as data:
                while chunk := data.read(1 << 20):
                    yield chunk

        resp = await self._http.post(
            url, params={"mode": mode, "properties": properties}, content=body(), timeout=None,
            headers={"content-type": "application/x-ndjson"},
        )
        if resp.status_code >= httpx.codes.BAD_REQUEST:
            raise GraphosError(resp.status_code, _extract_detail(resp), "POST", url)
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
        index: str | None = None,
        fuzzy: bool = False,
        min_score: float = 0.0,
        property: str | None = None,  # noqa: A002 — the query parameter's name
    ) -> SearchResponse:
        """Search nodes.  With ``index``: ranked by a search index of the tenant,
        each hit with a ``score`` and the property it ``matched``; without:
        unranked case-insensitive substring match on ``property`` (``name`` by
        default; other properties are read row by row)."""
        params: dict[str, Any] = {"q": q, "limit": limit}
        if label is not None:
            params["label"] = label
        if property is not None:
            params["property"] = property
        if index is not None:
            params["index"] = index
            params["fuzzy"] = "true" if fuzzy else "false"
            if min_score:
                params["min_score"] = min_score
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
        missing: bool = False,
    ) -> NodesAggregateResponse:
        """Group vertices of ``label`` by a property and count.

        Inheritance-aware: passing the parent label (e.g. ``Document``)
        matches all descendant labels via postgres table inheritance.
        Useful for live summary views (e.g. counts per ``type``) when
        no materialised snapshot exists. ``missing`` also counts the nodes
        without the property, under ``""``; ``total`` is every node counted
        (``None`` from a server older than the field).
        """
        body: dict[str, Any] = {
            "label": label, "group_by": group_by, "limit": limit,
        }
        if values:
            body["values"] = values
        if missing:
            body["missing"] = True
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

    async def delete_nodes(self, refs: Iterable[NodeRef]) -> DeleteNodesResponse:
        """Delete up to 1000 nodes, each by label and id, with their edges, in
        one transaction.  A node that does not exist is skipped.  The call is
        retried on a lost connection or a 5xx; a retry after the first attempt
        committed finds nothing left and reports zero deleted."""
        body = {"nodes": [r.model_dump() for r in refs]}
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/graph/nodes:delete", json_body=body,
        )
        return DeleteNodesResponse.model_validate(resp.json())

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
        limit: int = 50,
    ) -> CommunitiesResponse:
        """The tenant's communities, largest first: at most ``limit`` (up to 500;
        0 = no cap)."""
        params: dict[str, Any] = {
            "min_size": min_size, "include_members": include_members, "limit": limit,
        }
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

    async def graph_map(self, *, exact: bool = False) -> GraphMap:
        """``GET …/graph/map``: every label with its node count and the edge
        counts between labels; large edge tables are sampled unless ``exact``."""
        params = {"exact": "true"} if exact else None
        resp = await self._request("GET", f"/v1/{self._tenant}/graph/map", params=params)
        return GraphMap.model_validate(resp.json())

    async def map_hubs(self, label: str, *, limit: int = 40) -> HubsResponse:
        """``GET …/graph/map/hubs``: the label's best-connected nodes, most first."""
        resp = await self._request(
            "GET", f"/v1/{self._tenant}/graph/map/hubs", params={"label": label, "limit": limit},
        )
        return HubsResponse.model_validate(resp.json())

    async def community_map(self, *, limit: int = 150) -> CommunityMap:
        """``GET …/graph/communities/map``: the last recompute's communities,
        largest first, and the edges between them (``available`` is false until
        a recompute has run)."""
        resp = await self._request(
            "GET", f"/v1/{self._tenant}/graph/communities/map", params={"limit": limit},
        )
        return CommunityMap.model_validate(resp.json())

    async def tenant_stats(self) -> TenantStatsResponse:
        resp = await self._request("GET", f"/v1/{self._tenant}/graph/stats")
        return TenantStatsResponse.model_validate(resp.json())

    # ── Generic analytics primitives ───────────────────────────────────────

    async def catalogue(
        self, *, properties: list[str] | None = None, refresh: bool = False,
    ) -> dict[str, Any]:
        """``GET .../graph/catalogue``: property name → labels carrying it.

        Diagnostic for rule-based edges: shows which labels the resolver
        considers for each property.  ``properties`` narrows it; ``refresh``
        re-reads every node first, dropping properties no node carries.
        """
        params: dict[str, Any] = {"refresh": refresh}
        if properties:
            params["property"] = list(properties)
        resp = await self._request(
            "GET", f"/v1/{self._tenant}/graph/catalogue", params=params,
        )
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

    async def schema_summary(self) -> SchemaSummaryResponse:
        """The tenant's schema as a model needs it before querying: labels with
        counts and properties (declared type, rows, distinct values, the values
        when few), label pairs per edge type, search indexes, saved queries;
        ``.text`` is the compact form for a prompt."""
        resp = await self._request("GET", f"/v1/{self._tenant}/schema/summary")
        return SchemaSummaryResponse.model_validate(resp.json())

    async def check_edges(
        self, edges: Iterable[EdgeCheckItem], *, include_properties: bool = False,
    ) -> EdgesCheckResponse:
        """Check which of up to 1000 edges exist, each by type and the ids of
        its ends: one result per edge, in order, ``present`` (with its
        revision and ETag), ``absent`` or ``ambiguous`` (an end id is in
        several labels; ``src_label`` / ``dst_label`` pick one).  A read, so it
        is retried like one."""
        body = {
            "edges": [e.model_dump(exclude_none=True) for e in edges],
            "include_properties": include_properties,
        }
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/graph/edges:check", json_body=body,
        )
        return EdgesCheckResponse.model_validate(resp.json())

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

    # ── Change feed ────────────────────────────────────────────────────────
    #
    # Every write to the tenant, in an order that never skips a committed
    # change.  Records say what changed, not the new values; re-read those.
    # Delivery is at least once: after a reconnect a record can repeat.

    async def changes(self, after: str | None = None, *, limit: int = 100) -> ChangesPage:
        """One page of records after ``after`` (the oldest kept when None).

        Raises ``GraphosError`` 410 when the records after ``after`` were
        pruned: read the data again and follow from :meth:`changes_head`.
        """
        params: dict[str, Any] = {"limit": limit}
        if after is not None:
            params["after"] = after
        resp = await self._request("GET", f"/v1/{self._tenant}/changes", params=params)
        return ChangesPage.model_validate(resp.json())

    async def changes_history(
        self,
        before: str | None = None,
        *,
        ops: Sequence[str] | None = None,
        limit: int = 100,
    ) -> ChangesHistory:
        """Earlier records, newest first (before ``before``, or the newest):
        what happened, for showing it.  ``ops`` keeps only those ops.  Pass
        ``older`` back as ``before`` for the page before.  To follow the feed
        use :meth:`changes` or :meth:`stream_changes`."""
        params: dict[str, Any] = {"limit": limit}
        if before is not None:
            params["before"] = before
        if ops:
            params["op"] = list(ops)
        resp = await self._request("GET", f"/v1/{self._tenant}/changes/history", params=params)
        return ChangesHistory.model_validate(resp.json())

    async def changes_head(self) -> str:
        """The position to follow the feed from after reading the data afresh."""
        resp = await self._request("GET", f"/v1/{self._tenant}/changes/head")
        return str(resp.json()["cursor"])

    async def iter_changes(
        self, after: str | None = None, *, page_size: int = 500,
    ) -> AsyncIterator[ChangeItem]:
        """Every record after ``after`` that is visible now, page by page;
        stops when caught up.  The last record's ``cursor`` is where to
        continue."""
        while True:
            page = await self.changes(after, limit=page_size)
            for change in page.changes:
                yield change
            after = page.next
            if not page.more:
                return

    async def stream_changes(
        self,
        after: str | None = None,
        *,
        reconnect: bool = True,
        read_timeout_s: float = 60.0,
        max_backoff_s: float = 30.0,
    ) -> AsyncIterator[ChangeItem]:
        """Follow the feed live (Server-Sent Events), forever.

        After a dropped connection, a server restart or a 5xx, it reconnects
        with back-off and resumes after the last record it yielded
        (``Last-Event-ID``), so nothing is skipped.  ``read_timeout_s``
        without a byte (the server sends a keep-alive every 15 s) counts as
        a dropped connection.  Raises ``GraphosError`` on 410 (resync) and on
        any other 4xx.
        """
        last = after
        backoff = self._retry_backoff_s
        path = f"/v1/{self._tenant}/changes/stream"
        while True:
            headers = {"Last-Event-ID": last} if last else {}
            try:
                async with self._http.stream(
                    "GET", path, headers=headers,
                    timeout=httpx.Timeout(self._http.timeout.connect, read=read_timeout_s),
                ) as resp:
                    if resp.status_code >= httpx.codes.BAD_REQUEST:
                        await resp.aread()
                        error = GraphosError(resp.status_code, _extract_detail(resp), "GET", path)
                        if resp.status_code < httpx.codes.INTERNAL_SERVER_ERROR or not reconnect:
                            raise error
                        log.warning("change stream: %s; reconnecting", error)
                    else:
                        backoff = self._retry_backoff_s
                        async for event, data in _sse_events(resp):
                            if event == "change":
                                change = ChangeItem.model_validate_json(data)
                                last = change.cursor
                                yield change
                            elif event == "gone":
                                raise GraphosError(
                                    httpx.codes.GONE, json.loads(data)["detail"], "GET", path,
                                )
            except (httpx.TransportError, httpx.StreamError) as exc:
                if not reconnect:
                    raise GraphosError(0, f"change stream: {exc}", "GET", path) from exc
                log.warning("change stream dropped (%s); reconnecting", type(exc).__name__)
            if not reconnect:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff_s)

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

    async def compute_metrics(
        self,
        *,
        metrics: Iterable[str] = ("degree", "pagerank", "components"),
        rel_types: list[str] | None = None,
        node_filter: Mapping[str, Any] | None = None,
        damping: float = 0.85,
        dry_run: bool = False,
    ) -> ComputeMetricsResponse:
        """Compute graph metrics and store them on the nodes (maintain).

        ``metrics``: any of ``degree`` (graphos_degree, graphos_in_degree,
        graphos_out_degree), ``pagerank`` (graphos_pagerank), ``components``
        (graphos_component) and ``betweenness`` (graphos_betweenness, skipped
        on large graphs). Only changed values are written. ``graphos_*``
        properties are reserved: writes using them are refused.
        """
        body = _metrics_body(
            metrics=metrics, rel_types=rel_types, node_filter=node_filter,
            damping=damping, dry_run=dry_run,
        )
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/graph/metrics:compute", json_body=body,
            idempotent=False,   # long-running: never re-run on a 5xx
        )
        return ComputeMetricsResponse.model_validate(resp.json())

    async def submit_compute_metrics(
        self,
        *,
        metrics: Iterable[str] = ("degree", "pagerank", "components"),
        rel_types: list[str] | None = None,
        node_filter: Mapping[str, Any] | None = None,
        damping: float = 0.85,
        dry_run: bool = False,
    ) -> Job:
        """:meth:`compute_metrics` as a background job; its ``result`` is the
        :class:`ComputeMetricsResponse` body. One whole-graph job per tenant: a
        submission while a metrics run or a community recompute is queued or
        running raises 409."""
        body = _metrics_body(
            metrics=metrics, rel_types=rel_types, node_filter=node_filter,
            damping=damping, dry_run=dry_run,
        )
        return await self._submit(f"/v1/{self._tenant}/graph/metrics:compute", body)

    # ── Property definitions ──────────────────────────────────────────────

    async def property_definitions(self) -> PropertyDefinitionsResponse:
        """The property definitions every label and edge type is checked against,
        with the mode each one is checked in."""
        resp = await self._request("GET", f"/v1/{self._tenant}/graph/properties")
        return PropertyDefinitionsResponse.model_validate(resp.json())

    async def check_properties(
        self,
        *,
        labels: list[str] | None = None,
        types: list[str] | None = None,
        examples: int = 10,
    ) -> PropertiesCheckResponse:
        """Check the stored rows against the property definitions (maintain), as if
        every rule applied whatever the mode. Writes nothing. ``labels`` and
        ``types`` narrow it (all by default); ``examples`` ids are kept per rule."""
        resp = await self._request(
            "POST", f"/v1/{self._tenant}/graph/properties:check",
            json_body=_check_body(labels, types, examples),
        )
        return PropertiesCheckResponse.model_validate(resp.json())

    async def submit_check_properties(
        self,
        *,
        labels: list[str] | None = None,
        types: list[str] | None = None,
        examples: int = 10,
    ) -> Job:
        """:meth:`check_properties` as a background job; its ``result`` is the
        :class:`PropertiesCheckResponse` body."""
        return await self._submit(
            f"/v1/{self._tenant}/graph/properties:check", _check_body(labels, types, examples),
        )

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
        status: str | Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[Job]:
        """``GET /v1/jobs``: newest first, only jobs this token may see.
        ``status``: one state or several (``["queued", "running"]``: what is
        active now, synchronous calls included)."""
        params: dict[str, Any] = {"limit": limit}
        if tenant is not None:
            params["tenant"] = tenant
        if status is not None:
            params["status"] = [status] if isinstance(status, str) else list(status)
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


def _resolve_body(
    edge_types: Iterable[str] | None, mode: str, adopt_unmarked: bool, dry_run: bool,
) -> dict[str, Any]:
    body: dict[str, Any] = {} if edge_types is None else {"edge_types": list(edge_types)}
    if mode != "insert":
        body["mode"] = mode
    if adopt_unmarked:
        body["adopt_unmarked"] = True
    if dry_run:
        body["dry_run"] = True
    return body


def _vacuum_body(tables: list[str] | None, full: bool, analyze: bool) -> dict[str, Any]:
    body: dict[str, Any] = {"full": full, "analyze": analyze}
    if tables:
        body["tables"] = list(tables)
    return body


def _check_body(labels: list[str] | None, types: list[str] | None, examples: int) -> dict[str, Any]:
    body: dict[str, Any] = {"examples": examples}
    if labels is not None:
        body["labels"] = list(labels)
    if types is not None:
        body["types"] = list(types)
    return body


def _metrics_body(
    *,
    metrics: Iterable[str],
    rel_types: list[str] | None,
    node_filter: Mapping[str, Any] | None,
    damping: float,
    dry_run: bool,
) -> dict[str, Any]:
    body: dict[str, Any] = {"metrics": list(metrics), "damping": damping, "dry_run": dry_run}
    if rel_types:
        body["rel_types"] = rel_types
    if node_filter:
        body["node_filter"] = dict(node_filter)
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


async def _sse_events(resp: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """``(event, data)`` per Server-Sent Event; comments and ``retry`` skipped."""
    event, data = "message", []
    async for line in resp.aiter_lines():
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)


def _extract_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return resp.text[:200]

# bust-cache: 1779959546

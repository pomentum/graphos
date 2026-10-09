"""GraphosPool and the snapshot/operations methods, over a mock transport."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from graphos_client import (
    EdgeUpsert,
    GraphosClient,
    GraphosError,
    GraphosPool,
    NodeUpsert,
    TenantSchema,
    is_schema_drift,
)

_SCHEMA = TenantSchema(
    vertex_labels=["User", "Document"],
    edge_labels=["AUTHORED"],
    property_indexes=[{"label": "Document", "properties": ["kind"]}],
    generated_columns=[{"label": "Document", "column": "kind", "property": "kind"}],
)
_NODES_OK = {"tenant": "t", "nodes_upserted": 1, "by_label": {"User": 1}, "elapsed_ms": 1}


class _Server:
    """Records requests; optionally fails the first node write with schema drift."""

    def __init__(self, *, drift_once: bool = False) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._drift = drift_once

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        if request.url.path == "/v1/tenants":
            return httpx.Response(200, json={"tenant": body["name"]})
        if request.url.path.endswith("/graph/nodes"):
            if self._drift:
                self._drift = False
                return httpx.Response(400, json={"detail": "labels ['User'] are not registered"})
            return httpx.Response(200, json=_NODES_OK)
        return httpx.Response(200, json={})

    def registrations(self) -> list[dict]:
        return [b for m, p, b in self.calls if p == "/v1/tenants"]


def _pool(server: _Server) -> GraphosPool:
    return GraphosPool(
        base_url="http://x", token="tok", schema=_SCHEMA,
        transport=httpx.MockTransport(server), retry_backoff_s=0.0,
    )


@pytest.mark.asyncio
async def test_pool_registers_each_tenant_once_even_under_concurrency() -> None:
    server = _Server()
    async with _pool(server) as pool:
        clients = await asyncio.gather(*(pool.client("acme") for _ in range(10)))
        await pool.client("globex")
    assert len({id(c) for c in clients}) == 1
    regs = server.registrations()
    assert [r["name"] for r in regs] == ["acme", "globex"]
    # The same declarations for every tenant; no SQL is sent.
    for reg in regs:
        assert reg["property_indexes"] == [{"label": "Document", "properties": ["kind"]}]
        assert reg["generated_columns"] == [
            {"label": "Document", "column": "kind", "property": "kind"},
        ]
        assert "indexes" not in reg


@pytest.mark.asyncio
async def test_pool_call_reregisters_once_on_schema_drift() -> None:
    server = _Server(drift_once=True)
    async with _pool(server) as pool:
        resp = await pool.call(
            "acme", lambda c: c.upsert_nodes([NodeUpsert(id="u1", label="User")]),
        )
    assert resp.nodes_upserted == 1
    assert len(server.registrations()) == 2          # first use + one re-registration


@pytest.mark.asyncio
async def test_pool_call_does_not_retry_other_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/tenants":
            return httpx.Response(200, json={})
        return httpx.Response(403, json={"detail": "token has no access to tenant 'acme'"})

    pool = GraphosPool(base_url="http://x", schema=_SCHEMA, transport=httpx.MockTransport(handler))
    with pytest.raises(GraphosError) as info:
        await pool.call("acme", lambda c: c.upsert_nodes([NodeUpsert(id="u1", label="User")]))
    assert info.value.status_code == 403
    await pool.close()


def test_is_schema_drift() -> None:
    assert is_schema_drift(GraphosError(400, "vertex labels ['X'] not registered", "POST", "/"))
    assert is_schema_drift(GraphosError(404, "tenant 'acme' is not registered.", "GET", "/"))
    assert not is_schema_drift(GraphosError(500, "not registered", "POST", "/"))
    assert not is_schema_drift(ValueError("not registered"))
    # A write refused by declared endpoints (strict_endpoints) is not drift:
    # registering again would not help.
    refused = (
        "Repo-[DEFINES]->Class is not declared: DEFINES may join File->Symbol. "
        "Declare the pair (edge_endpoints on POST /v1/tenants) or turn strict_endpoints off."
    )
    assert not is_schema_drift(GraphosError(400, refused, "POST", "/"))


@pytest.mark.asyncio
async def test_snapshot_import_sends_nodes_then_edges_as_ndjson() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["type"] = request.headers["content-type"]
        seen["lines"] = [json.loads(x) for x in request.content.decode().splitlines()]
        return httpx.Response(200, json={"tenant": "acme", "nodes_upserted": 1})

    async with GraphosClient(
        base_url="http://x", tenant="acme", transport=httpx.MockTransport(handler),
    ) as cli:
        await cli.snapshot_import(
            nodes=[NodeUpsert(id="u1", label="User")],
            edges=[EdgeUpsert(src_id="u1", src_label="User", dst_id="u1",
                              dst_label="User", type="FOLLOWS")],
        )
    assert seen["path"] == "/v1/acme/graph/snapshot-import"
    assert seen["type"] == "application/x-ndjson"
    assert [line["_kind"] for line in seen["lines"]] == ["node", "edge"]

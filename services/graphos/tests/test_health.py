"""Health endpoint smoke tests (no DB required).

These exercise routing and response shape. The lifespan hook tries to open
an asyncpg pool at startup, which fails without a real graphos-db; we work
around that by inspecting routes without entering the TestClient as a
context manager (which is what triggers lifespan).
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi.testclient import TestClient

from graphos.app import create_app
from graphos.config import Settings


def _bare_app() -> object:
    settings = Settings(db_password="placeholder", auth_disabled=True)
    return create_app(settings=settings)


def test_root_returns_metadata() -> None:
    app = _bare_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/")
    assert resp.status_code == HTTPStatus.OK
    body = resp.json()
    assert body["service"] == "graphos"
    assert "version" in body


def test_healthz_returns_ok() -> None:
    app = _bare_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/healthz")
    assert resp.status_code == HTTPStatus.OK
    assert resp.json() == {"status": "ok"}


def test_readyz_not_ok_without_lifespan() -> None:
    """Without a real DB, readyz must NOT report ready."""
    app = _bare_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/readyz")
    assert resp.status_code != HTTPStatus.OK


def test_openapi_routes() -> None:
    """Sanity-check the published route surface."""
    app = _bare_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/openapi.json")
    assert resp.status_code == HTTPStatus.OK
    paths = resp.json()["paths"]
    # Tenant management
    assert "/v1/tenants" in paths
    assert "/v1/tenants/{name}" in paths
    # DELETE /v1/tenants/{name} shares path with GET; FastAPI groups methods
    # under the same path entry. Method check:
    assert "delete" in paths["/v1/tenants/{name}"]
    # Info
    assert "/v1/info" in paths
    # Graph data-plane
    assert "/v1/{tenant}/graph/search" in paths
    assert "/v1/{tenant}/graph/nodes" in paths
    assert "/v1/{tenant}/graph/edges" in paths
    assert "/v1/{tenant}/graph/nodes:bulk" in paths
    assert "/v1/{tenant}/graph/edges:bulk" in paths
    assert "/v1/{tenant}/graph/neighbors" in paths
    assert "/v1/{tenant}/graph/induced_edges" in paths
    assert "/v1/{tenant}/graph/expand" in paths
    assert "/v1/{tenant}/graph/edges/aggregate" in paths
    assert "/v1/{tenant}/recompute-communities" in paths
    # Community reads + tenant stats
    assert "/v1/{tenant}/graph/communities" in paths
    assert "/v1/{tenant}/graph/community-summary" in paths
    assert "/v1/{tenant}/graph/stats" in paths
    # Graph export
    assert "/v1/{tenant}/graph/export" in paths
    assert "/v1/{tenant}/graph/snapshot-import" in paths
    assert "/v1/{tenant}/graph/nodes/{node_id}" in paths
    # tombstone-by-property (DELETE on /nodes, distinguished from POST)
    assert "/v1/{tenant}/graph/nodes" in paths
    # Operational
    assert "/v1/db/stats" in paths
    assert "/v1/db/slow-queries" in paths
    assert "/v1/db/vacuum" in paths
    # No /admin URL segment — admin scope is a token attribute, not a path.
    assert not any(p.startswith("/v1/admin") for p in paths)

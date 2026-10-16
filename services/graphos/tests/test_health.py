"""Health endpoint smoke tests (no DB required).

These exercise routing and response shape. The lifespan hook tries to open
an asyncpg pool at startup, which fails without a real graphos-db; we work
around that by inspecting routes without entering the TestClient as a
context manager (which is what triggers lifespan).
"""

from __future__ import annotations

import re
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


def test_the_api_documentation_is_served_and_listed() -> None:
    app = _bare_app()
    client = TestClient(app, raise_server_exceptions=False)
    links = client.get("/").json()
    for page in ("docs", "redoc", "openapi"):
        assert client.get(links[page]).status_code == HTTPStatus.OK, page


def test_the_documentation_pages_load_nothing_from_elsewhere() -> None:
    """Swagger UI and ReDoc come from graphos's own files: no CDN, no fonts."""
    client = TestClient(_bare_app(), raise_server_exceptions=False)
    for page in ("/docs", "/redoc"):
        html = client.get(page).text
        assert "https://" not in html, page
        if page == "/docs":
            assert '"validatorUrl": null' in html    # no badge from validator.swagger.io
        for asset in re.findall(r'(?:src|href)="(/api-docs-assets/[^"]+)"', html):
            assert client.get(asset).status_code == HTTPStatus.OK, asset


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
    assert "post" in paths["/v1/{tenant}/graph/nodes:delete"]
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
    assert "delete" in paths["/v1/{tenant}/graph/nodes/{external_id}"]
    # tombstone-by-property (DELETE on /nodes, distinguished from POST)
    assert "/v1/{tenant}/graph/nodes" in paths
    # Operational
    assert "/v1/db/stats" in paths
    assert "/v1/db/slow-queries" in paths
    assert "/v1/db/vacuum" in paths
    # No /admin URL segment — admin scope is a token attribute, not a path.
    assert not any(p.startswith("/v1/admin") for p in paths)

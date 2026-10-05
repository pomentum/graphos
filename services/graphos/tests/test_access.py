"""Every operation declares the access it needs, and tokens are held to it.

The declarations are read from the OpenAPI schema, where each operation
names the bearer scope it needs (``read`` / ``write``); the schema is the
public contract and does not depend on how FastAPI stores its routes.

Pure Python: the app is built without its lifespan, so nothing touches a
database.  A request that gets past auth may then fail for want of a pool or
a registered tenant; these tests only look at whether auth let it through.
"""

from __future__ import annotations

import json
import logging
import re
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graphos.app import create_app
from graphos.auth import Access, TokenStore
from graphos.config import Settings

TOKENS = {
    "tok_read": {"consumer": "reader", "tenants": ["acme"], "access": "read"},
    "tok_write": {"consumer": "writer", "tenants": ["acme"], "access": "write"},
    "tok_admin_read": {"consumer": "ops-ro", "tenants": [], "admin": True, "access": "read"},
    "tok_admin": {"consumer": "ops", "tenants": [], "admin": True},   # no "access"
}
# The only operations anyone may call without a token.
PUBLIC_PATHS = {"/", "/healthz", "/readyz", "/v1/info"}


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps(TOKENS))
    app = create_app(Settings(db_password="placeholder", auth_tokens_file=tokens))
    app.state.token_store = TokenStore(tokens)   # lifespan is not run in unit tests
    return app


def _operations(app: FastAPI) -> list[tuple[str, str, Access | None]]:
    """``(method, path, declared access)`` of every operation in the schema."""
    out = []
    for path, ops in app.openapi()["paths"].items():
        for method, op in ops.items():
            scopes = {s for req in op.get("security", []) for v in req.values() for s in v}
            access = Access(scopes.pop()) if len(scopes) == 1 else None
            out.append((method.upper(), path, access))
    return out


def _url(path: str, tenant: str) -> str:
    def fill(m: re.Match[str]) -> str:
        if m[1] == "tenant":
            return tenant
        if m[1] == "job_id":
            return "00000000-0000-0000-0000-000000000000"
        return "x"
    return re.sub(r"\{(\w+)\}", fill, path)


def _call(client: TestClient, method: str, path: str, token: str, tenant: str = "acme"):  # noqa: ANN202
    return client.request(
        method, _url(path, tenant), headers={"Authorization": f"Bearer {token}"},
    )


def test_every_operation_declares_read_or_write(app: FastAPI) -> None:
    undeclared = sorted(
        f"{m} {p}" for m, p, access in _operations(app)
        if access is None and p not in PUBLIC_PATHS
    )
    assert undeclared == [], "take the principal as ReadPrincipal or WritePrincipal"


def test_only_the_expected_operations_are_public(app: FastAPI) -> None:
    public = {p for _, p, access in _operations(app) if access is None}
    assert public == PUBLIC_PATHS


def test_every_tenant_operation_enforces_the_tenant_scope(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    tenant_ops = [(m, p) for m, p, _ in _operations(app) if "{tenant}" in p]
    assert len(tenant_ops) > 30
    for method, path in tenant_ops:
        resp = _call(client, method, path, "tok_write", tenant="other")
        assert resp.status_code == HTTPStatus.FORBIDDEN, (method, path, resp.text)
        assert "no access to tenant" in resp.json()["detail"], (method, path)


def test_read_only_token_is_refused_every_write_operation(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    writes = [(m, p) for m, p, access in _operations(app) if access is Access.WRITE]
    assert len(writes) > 15
    for method, path in writes:
        # Admin does not lift read-only.
        resp = _call(client, method, path, "tok_admin_read")
        assert resp.status_code == HTTPStatus.FORBIDDEN, (method, path, resp.text)
        assert resp.json()["detail"] == "this token is read-only", (method, path)


def test_read_only_token_passes_auth_on_every_read_operation(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    for method, path, access in _operations(app):
        if access is not Access.READ:
            continue
        resp = _call(client, method, path, "tok_admin_read")
        assert resp.status_code not in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN), (
            method, path, resp.text,
        )


def test_reads_sent_as_post_are_reads(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer tok_read"}
    for path in ("nodes:scan", "edges:scan", "edges/aggregate", "induced_edges"):
        resp = client.post(f"/v1/acme/graph/{path}", headers=headers, json={})
        assert resp.status_code != HTTPStatus.FORBIDDEN, path
    resp = client.post("/v1/acme/graph/nodes", headers=headers, json={"nodes": []})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_missing_or_unknown_token_is_401(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/v1/acme/graph/search?q=x").status_code == HTTPStatus.UNAUTHORIZED
    resp = client.get("/v1/acme/graph/search?q=x", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    resp = client.get("/v1/acme/graph/search?q=x", headers={"Authorization": "Basic abc"})
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


def test_audit_follows_the_declared_access(
    app: FastAPI, caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    admin = {"Authorization": "Bearer tok_admin"}
    with caplog.at_level(logging.INFO, logger="graphos.audit"):
        client.post("/v1/acme/graph/nodes:scan", headers=admin, json={})        # read
        client.get("/v1/acme/graph/nodes/x", headers=admin)                     # read
        client.delete("/v1/acme/graph/edges/T?src=a&dst=b", headers=admin)      # write
        client.patch("/v1/acme/graph/nodes/x", headers=admin, json={"a": 1})    # write
        client.post("/v1/acme/graph/nodes", json={"nodes": []})                 # write, no token
    audited = [(r.method, r.path, r.status) for r in caplog.records if r.name == "graphos.audit"]
    assert audited == [
        ("DELETE", "/v1/acme/graph/edges/T", HTTPStatus.NOT_FOUND),
        ("PATCH", "/v1/acme/graph/nodes/x", HTTPStatus.NOT_FOUND),
        ("POST", "/v1/acme/graph/nodes", HTTPStatus.UNAUTHORIZED),
    ]


def test_token_without_access_defaults_to_write_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps(TOKENS))
    with caplog.at_level(logging.WARNING, logger="graphos.auth"):
        store = TokenStore(tokens)
    assert store.lookup("tok_admin").access is Access.WRITE
    assert store.lookup("tok_read").access is Access.READ
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.consumers == ["ops"]
    assert "tok_admin" not in caplog.text   # never the token itself


def test_invalid_access_skips_only_that_token(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({
        "tok_bad": {"consumer": "bad", "tenants": [], "access": "admin"},
        "tok_ok": {"consumer": "ok", "tenants": [], "access": "read"},
    }))
    store = TokenStore(tokens)
    assert store.lookup("tok_bad") is None
    assert store.lookup("tok_ok") is not None
    assert store.load_error is None

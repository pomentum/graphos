"""Every operation declares the access it needs, and callers are held to it.

The declarations are read from the OpenAPI schema, where each operation
names the bearer scope it needs (``read``, ``write``, ``maintain``,
``owner`` on its tenant, or ``admin``); the schema is the public contract
and does not depend on how FastAPI stores its routes.

Pure Python: the app is built without its lifespan, and callers are fixed
principals (``StaticAuthenticator``), so nothing touches a database.  A
request that gets past auth may then fail for want of a pool or a registered
tenant; these tests only look at whether auth let it through.
"""

from __future__ import annotations

import logging
import re
from http import HTTPStatus

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graphos.app import create_app
from graphos.auth import Access, StaticAuthenticator
from graphos.config import Settings
from graphos.identity import Level, Principal

PRINCIPALS = {
    "tok_read": Principal.static("reader", tenants={"acme": "read"}),
    "tok_write": Principal.static("writer", tenants={"acme": "write"}),
    "tok_maintain": Principal.static("maintainer", tenants={"acme": "maintain"}),
    "tok_owner": Principal.static("owner", tenants={"acme": "owner"}),
    "tok_pattern": Principal.static("team", tenants={"ac*": "write"}),
    "tok_creator": Principal.static("svc", create_tenants=["sol_*"]),
    "tok_admin": Principal.static("ops", admin=True),
    "tok_must_change": Principal(
        consumer="admin", subject="user:admin", admin=True, must_change=True,
    ),
}
# The only operations anyone may call without a token.
PUBLIC_PATHS = {
    "/", "/healthz", "/readyz", "/v1/info",
    "/v1/auth/login", "/v1/auth/refresh", "/v1/auth/logout",
    "/v1/auth/config", "/v1/auth/oidc/start", "/v1/auth/oidc/callback",
}
# The token holding exactly each level on "acme".
AT_LEVEL = {
    Level.READ: "tok_read", Level.WRITE: "tok_write",
    Level.MAINTAIN: "tok_maintain", Level.OWNER: "tok_owner",
}


@pytest.fixture
def app() -> FastAPI:
    app = create_app(Settings(db_password="placeholder"))
    app.state.authenticator = StaticAuthenticator(PRINCIPALS)   # lifespan is not run
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
        if m[1] in ("tenant", "name") and path.startswith("/v1/tenants") or m[1] == "tenant":
            return tenant
        if m[1] == "job_id":
            return "00000000-0000-0000-0000-000000000000"
        return "x"
    return re.sub(r"\{(\w+)\}", fill, path)


def _call(client: TestClient, method: str, path: str, token: str, tenant: str = "acme"):  # noqa: ANN202
    return client.request(
        method, _url(path, tenant), headers={"Authorization": f"Bearer {token}"},
    )


def _passed_auth(resp) -> bool:  # noqa: ANN001
    return resp.status_code not in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)


def test_every_operation_declares_its_access(app: FastAPI) -> None:
    undeclared = sorted(
        f"{m} {p}" for m, p, access in _operations(app)
        if access is None and p not in PUBLIC_PATHS
    )
    assert undeclared == [], "take the principal as one of the *Principal aliases"


def test_only_the_expected_operations_are_public(app: FastAPI) -> None:
    public = {p for _, p, access in _operations(app) if access is None}
    assert public == PUBLIC_PATHS


def test_every_tenant_operation_enforces_the_tenant(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    tenant_ops = [(m, p) for m, p, _ in _operations(app) if "{tenant}" in p]
    assert len(tenant_ops) > 30
    for method, path in tenant_ops:
        resp = _call(client, method, path, "tok_owner", tenant="other")
        assert resp.status_code == HTTPStatus.FORBIDDEN, (method, path, resp.text)
        assert "no access to tenant" in resp.json()["detail"], (method, path)


def test_each_tenant_operation_needs_its_level_and_no_more(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    checked = 0
    for method, path, access in _operations(app):
        if "{tenant}" not in path or access is None or access.level is None:
            continue
        needed = access.level
        assert _passed_auth(_call(client, method, path, AT_LEVEL[needed])), (method, path)
        if needed > Level.READ:
            resp = _call(client, method, path, AT_LEVEL[Level(needed - 1)])
            assert resp.status_code == HTTPStatus.FORBIDDEN, (method, path, resp.text)
            assert f"'{needed.label}' access" in resp.json()["detail"], (method, path)
        checked += 1
    assert checked > 30


def test_a_pattern_grants_every_matching_tenant(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer tok_pattern"}
    assert _passed_auth(client.post("/v1/acme/graph/nodes", headers=headers, json={"nodes": []}))
    resp = client.post("/v1/zeta/graph/nodes", headers=headers, json={"nodes": []})
    assert resp.status_code == HTTPStatus.FORBIDDEN


def test_admin_operations_need_admin(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    admin_ops = [(m, p) for m, p, access in _operations(app) if access is Access.ADMIN]
    assert {p for _, p in admin_ops} >= {"/v1/db/stats", "/v1/db/vacuum", "/v1/users"}
    for method, path in admin_ops:
        resp = _call(client, method, path, "tok_owner")
        assert resp.status_code == HTTPStatus.FORBIDDEN, (method, path, resp.text)
        assert resp.json()["detail"] == "admin access required"
        assert _passed_auth(_call(client, method, path, "tok_admin")), (method, path)


def test_recompute_and_metrics_need_maintain(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    for path in ("/v1/acme/recompute-communities", "/v1/acme/graph/metrics:compute"):
        writer = client.post(path, headers={"Authorization": "Bearer tok_write"}, json={})
        assert writer.status_code == HTTPStatus.FORBIDDEN, path
        maintainer = client.post(path, headers={"Authorization": "Bearer tok_maintain"}, json={})
        assert _passed_auth(maintainer), path


def test_tenant_management_follows_create_maintain_and_own(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)

    def register(token: str, name: str):  # noqa: ANN202
        return client.post(
            "/v1/tenants", headers={"Authorization": f"Bearer {token}"}, json={"name": name},
        )

    def as_(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    # Creating: create_tenants on a matching pattern, or admin.
    assert register("tok_write", "sol_1").status_code == HTTPStatus.FORBIDDEN
    assert register("tok_creator", "x_1").status_code == HTTPStatus.FORBIDDEN
    assert _passed_auth(register("tok_creator", "sol_1"))
    # Re-registering acme: maintain on it.
    assert register("tok_write", "acme").status_code == HTTPStatus.FORBIDDEN
    assert _passed_auth(register("tok_maintain", "acme"))
    # Removing a declared pair: maintain; unregistering: owner.
    pair = "/v1/tenants/acme/edge_endpoints?type=T&src=A&dst=B"
    assert client.delete(pair, headers=as_("tok_write")).status_code == 403
    assert _passed_auth(client.delete(pair, headers=as_("tok_maintain")))
    assert client.delete("/v1/tenants/acme", headers=as_("tok_maintain")).status_code == 403
    assert _passed_auth(client.delete("/v1/tenants/acme", headers=as_("tok_owner")))
    # Inspecting the spec: maintain.
    assert client.get("/v1/tenants/acme", headers=as_("tok_write")).status_code == 403


def test_a_password_that_must_change_allows_only_changing_it(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer tok_must_change"}
    resp = client.get("/v1/acme/graph/search?q=x", headers=headers)
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert "change your password first" in resp.json()["detail"]
    assert client.get("/v1/users", headers=headers).status_code == HTTPStatus.FORBIDDEN
    me = client.get("/v1/me", headers=headers)
    assert me.status_code == HTTPStatus.OK and me.json()["must_change"] is True


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


def test_no_authenticator_fails_closed() -> None:
    app = create_app(Settings(db_password="placeholder"))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/v1/me", headers={"Authorization": "Bearer anything"})
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


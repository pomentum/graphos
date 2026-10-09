"""Security-relevant behaviour: timeouts, fail-closed auth, audit identity.

Registration declares indexes and columns instead of sending SQL; see
test_declared.py."""

from __future__ import annotations

import logging
from http import HTTPStatus

import asyncpg
import pytest
from fastapi.testclient import TestClient

from graphos.app import create_app
from graphos.auth import StaticAuthenticator
from graphos.config import Settings
from graphos.identity import Principal

_BASE = {"name": "demo", "vertex_labels": ["Doc"], "edge_labels": ["LINKS"]}


# ── statement timeout → 504 ────────────────────────────────────────────────


def test_statement_timeout_maps_to_504_problem_json() -> None:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))

    @app.get("/boom")
    async def boom() -> None:
        raise asyncpg.exceptions.QueryCanceledError("canceling statement due to statement timeout")

    resp = TestClient(app).get("/boom")
    assert resp.status_code == HTTPStatus.GATEWAY_TIMEOUT
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["status"] == HTTPStatus.GATEWAY_TIMEOUT


# ── auth fails closed ──────────────────────────────────────────────────────


def test_requests_rejected_without_tokens_or_explicit_opt_out() -> None:
    app = create_app(Settings(db_password="placeholder"))
    resp = TestClient(app).get("/v1/tenants")
    assert resp.status_code == HTTPStatus.UNAUTHORIZED


# ── audit names the real consumer ──────────────────────────────────────────


def test_audit_records_the_consumer(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(Settings(db_password="placeholder"))
    app.state.authenticator = StaticAuthenticator({"tok_ops": Principal.static("ops", admin=True)})
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="graphos.audit"):
        client.post("/v1/tenants", headers={"Authorization": "Bearer tok_ops"}, json={"name": "t1"})
    (record,) = [r for r in caplog.records if r.name == "graphos.audit"]
    assert record.consumer == "ops"

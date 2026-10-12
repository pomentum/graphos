"""Tests for the observability middlewares + traceparent propagation."""

from __future__ import annotations

import hashlib
import logging
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from graphos.app import create_app
from graphos.config import Settings
from graphos.observability.audit import AuditMiddleware, _extract_tenant_from_path
from graphos.observability.logging import _parse_traceparent


def _client(**overrides) -> TestClient:
    settings = Settings(
        db_password="placeholder", auth_disabled=True,
        **overrides,
    )
    app = create_app(settings=settings)
    return TestClient(app, raise_server_exceptions=False)


# ── traceparent parser ────────────────────────────────────────────────────


def test_parse_traceparent_valid() -> None:
    tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    assert _parse_traceparent(tp) == "0af7651916cd43dd8448eb211c80319c"


def test_parse_traceparent_invalid() -> None:
    assert _parse_traceparent("garbage") is None
    assert _parse_traceparent("") is None
    # All-zero trace_id is reserved-as-invalid per spec.
    assert _parse_traceparent("00-" + "0" * 32 + "-" + "1" * 16 + "-01") is None


def test_parse_traceparent_trims_whitespace() -> None:
    tp = "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-1111111111111111-01"
    assert _parse_traceparent(f"  {tp}  ") == "a" * 32


# ── traceparent round-trip via middleware ─────────────────────────────────


def test_traceparent_is_echoed_back_in_response() -> None:
    tp = "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-1111111111111111-01"
    client = _client()
    resp = client.get("/healthz", headers={"traceparent": tp})
    assert resp.status_code == HTTPStatus.OK
    assert resp.headers.get("traceparent") == tp


def test_no_traceparent_means_no_response_header() -> None:
    client = _client()
    resp = client.get("/healthz")
    assert resp.status_code == HTTPStatus.OK
    assert "traceparent" not in resp.headers


def test_invalid_traceparent_does_not_echo() -> None:
    client = _client()
    resp = client.get("/healthz", headers={"traceparent": "not-a-valid-header"})
    assert resp.status_code == HTTPStatus.OK
    # Invalid traceparent must not propagate forward.
    assert "traceparent" not in resp.headers


# ── audit helpers ─────────────────────────────────────────────────────────


def _audited_app() -> Starlette:
    """A route that reads its body as a stream, behind the audit middleware."""

    async def upload(request: Request) -> PlainTextResponse:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
        return PlainTextResponse(str(size))

    app = Starlette(routes=[Route("/v1/demo/graph/nodes:bulk", upload, methods=["POST"])])
    app.add_middleware(AuditMiddleware, enabled=True, writes_only=False)
    return app


def test_a_streamed_body_is_hashed_as_the_endpoint_reads_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = b"".join(b'{"id": "n%d", "label": "Doc"}\n' % i for i in range(5000))

    def chunks():  # noqa: ANN202 — sent chunked, as a streamed upload is
        for i in range(0, len(body), 4096):
            yield body[i:i + 4096]

    audit = caplog.at_level(logging.INFO, logger="graphos.audit")
    with audit, TestClient(_audited_app()) as client:
        assert client.post("/v1/demo/graph/nodes:bulk", content=chunks()).text == str(len(body))
        client.post("/v1/demo/graph/nodes:bulk", content=b"")
    hashes = [r.args_hash for r in caplog.records if r.name == "graphos.audit"]
    assert hashes == [hashlib.sha256(body).hexdigest()[:16], ""]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/demo/graph/nodes", "demo"),
        ("/v1/demo/graph/edges/aggregate", "demo"),
        ("/v1/pmcatalog/graph/search", "pmcatalog"),
        # graphos-reserved roots — not a tenant:
        ("/v1/info", None),
        ("/v1/tenants", None),
        ("/v1/tenants/demo", None),
        ("/v1/db/stats", None),
        # Non-versioned paths:
        ("/healthz", None),
        ("/metrics", None),
    ],
)
def test_extract_tenant_from_path(path: str, expected: str | None) -> None:
    assert _extract_tenant_from_path(path) == expected


# ── audit log fires on writes (via caplog) ─────────────────────────────────


def test_audit_log_fires_on_post(caplog: pytest.LogCaptureFixture) -> None:
    """A POST should produce one structured audit record."""
    client = _client()
    with caplog.at_level(logging.INFO, logger="graphos.audit"):
        client.post(
            "/v1/tenants",
            headers={"Authorization": "Bearer tok_replace_me_admin"},
            json={"name": "t1"},
        )
    audit_records = [r for r in caplog.records if r.name == "graphos.audit"]
    assert len(audit_records) == 1
    assert audit_records[0].method == "POST"
    assert audit_records[0].path == "/v1/tenants"


def test_audit_log_records_writes_that_crash(caplog: pytest.LogCaptureFixture) -> None:
    """A write whose handler raises is still audited, as a 500.

    Unit tests run without a database, so registering a tenant fails inside
    the handler — exactly the case an audit trail must not lose.
    """
    client = _client()
    with caplog.at_level(logging.INFO, logger="graphos.audit"):
        resp = client.post("/v1/tenants", json={"name": "t1"})
    assert resp.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    (record,) = [r for r in caplog.records if r.name == "graphos.audit"]
    assert record.status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert record.args_hash


def test_audit_log_does_not_fire_on_get(caplog: pytest.LogCaptureFixture) -> None:
    client = _client()
    with caplog.at_level(logging.INFO, logger="graphos.audit"):
        client.get("/healthz")
    audit_records = [r for r in caplog.records if r.name == "graphos.audit"]
    assert audit_records == []


def test_rendered_audit_line_keeps_extra_fields() -> None:
    """The JSON line on stdout must carry the audit fields, not just the event."""
    from graphos.observability.logging import configure_logging

    configure_logging(level="INFO", json_output=True)
    handler = next(h for h in logging.getLogger().handlers if h.get_name() == "graphos_structured")
    record = logging.LogRecord("graphos.audit", logging.INFO, __file__, 1, "write", None, None)
    record.consumer = "ops"
    record.status = 201
    line = handler.format(record)
    assert '"consumer": "ops"' in line
    assert '"status": 201' in line

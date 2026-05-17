"""Tests for the observability middlewares + traceparent propagation."""

from __future__ import annotations

import logging
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from graphos.app import create_app
from graphos.config import Settings
from graphos.observability.audit import _extract_tenant_from_path, _hash_body
from graphos.observability.logging import _parse_traceparent


def _client(**overrides) -> TestClient:
    settings = Settings(
        db_password="placeholder", auth_tokens_file=None, auth_disabled=True,
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


def test_hash_body_empty_is_empty() -> None:
    assert _hash_body(b"") == ""


def test_hash_body_is_16_hex_chars() -> None:
    h = _hash_body(b'{"a": 1}')
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_body_stable_across_calls() -> None:
    assert _hash_body(b"abc") == _hash_body(b"abc")


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

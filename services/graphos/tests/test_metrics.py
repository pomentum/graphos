"""Tests for the Prometheus /metrics endpoint and request instrumentation."""

from __future__ import annotations

from http import HTTPStatus

from fastapi.testclient import TestClient

from graphos.app import create_app
from graphos.config import Settings


def _client() -> TestClient:
    settings = Settings(db_password="placeholder", auth_tokens_file=None, auth_disabled=True)
    app = create_app(settings=settings)
    return TestClient(app, raise_server_exceptions=False)


def test_metrics_endpoint_serves_prometheus_text() -> None:
    client = _client()
    # Drive at least one request through the middleware so a counter is set.
    client.get("/healthz")

    resp = client.get("/metrics")
    assert resp.status_code == HTTPStatus.OK
    # Prometheus text format
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    # Standard exposition format markers
    assert "# HELP graphos_requests_total" in body
    assert "# TYPE graphos_requests_total counter" in body
    assert "# HELP graphos_request_duration_seconds" in body
    # The /healthz request just made should appear with method=GET and the
    # route template, not a literal URL.
    assert "graphos_requests_total" in body
    assert 'route="/healthz"' in body


def test_metrics_endpoint_not_instrumented_recursively() -> None:
    """/metrics itself must NOT increment graphos_requests_total — otherwise
    every scrape would skew the time series."""
    client = _client()
    body_before = client.get("/metrics").text

    # Pull twice; the route="/metrics" counter shouldn't appear with
    # any positive value (we exempt /metrics from the middleware).
    body_after = client.get("/metrics").text

    # Neither response should contain a /metrics row in graphos_requests_total
    # (because /metrics short-circuits the middleware).
    assert 'graphos_requests_total{method="GET",route="/metrics"' not in body_before
    assert 'graphos_requests_total{method="GET",route="/metrics"' not in body_after


def test_metrics_endpoint_in_openapi() -> None:
    """The /metrics endpoint should NOT be in /openapi.json — it's added
    via add_route() (not include_router()), so it doesn't appear in the
    typed API schema. We assert this so consumers don't accidentally
    treat /metrics as a public typed endpoint."""
    client = _client()
    resp = client.get("/openapi.json")
    assert resp.status_code == HTTPStatus.OK
    assert "/metrics" not in resp.json()["paths"]

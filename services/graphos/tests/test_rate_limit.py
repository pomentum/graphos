"""Per-caller rate-limiter tests.

Pure-Python — uses TestClient and a synthetic clock for the bucket so
we don't actually wait one second of wall time per scenario.
"""

from __future__ import annotations

import hashlib
from http import HTTPStatus

from fastapi.testclient import TestClient

from graphos.app import create_app
from graphos.auth import StaticAuthenticator
from graphos.config import Settings
from graphos.identity import Principal
from graphos.observability.rate_limit import (
    _BucketRegistry,
    _short_id,
)

# ── Bucket unit tests (synthetic clock) ────────────────────────────────────


def test_bucket_admits_under_cap() -> None:
    reg = _BucketRegistry(cap=3, window_s=1.0)
    b = reg.get("t1")
    for i in range(3):
        admitted, retry = b.admit(now=100.0 + i * 0.1)
        assert admitted
        assert retry == 0.0


def test_bucket_rejects_over_cap() -> None:
    reg = _BucketRegistry(cap=3, window_s=1.0)
    b = reg.get("t1")
    for i in range(3):
        b.admit(now=100.0 + i * 0.1)
    admitted, retry = b.admit(now=100.4)
    assert not admitted
    # Oldest in-window request was at 100.0 → ages out at 101.0;
    # we asked at 100.4 → retry should be ~0.6 s.
    assert 0.5 < retry < 0.7


def test_bucket_window_slides() -> None:
    """Old requests age out, freeing up capacity."""
    reg = _BucketRegistry(cap=2, window_s=1.0)
    b = reg.get("t1")
    assert b.admit(now=100.0)[0] is True
    assert b.admit(now=100.5)[0] is True
    assert b.admit(now=100.6)[0] is False              # at cap

    # The 100.0 request ages out at 101.0.
    assert b.admit(now=101.01)[0] is True


def test_separate_tokens_have_separate_buckets() -> None:
    reg = _BucketRegistry(cap=1, window_s=1.0)
    a = reg.get("token_a")
    b = reg.get("token_b")
    assert a.admit(now=100.0)[0] is True
    assert b.admit(now=100.0)[0] is True   # not blocked by a's bucket


# ── caller id ──────────────────────────────────────────────────────────────


def test_caller_id_hides_the_value_and_separates_shared_prefixes() -> None:
    a, b = _short_id("svc_shared_AAAA"), _short_id("svc_shared_BBBB")
    assert a != b and len(a) == 12
    assert a == hashlib.sha256(b"svc_shared_AAAA").hexdigest()[:12]
    assert "svc" not in a


def test_signed_in_callers_are_bucketed_by_user_not_by_token() -> None:
    settings = Settings(
        db_password="placeholder", rate_limit_enabled=True, rate_limit_rps=2,
        rate_limit_window_s=60.0,
    )
    app = create_app(settings=settings)
    same = Principal.static("svc", admin=True)
    app.state.authenticator = StaticAuthenticator({"tok_1": same, "tok_2": same})
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/v1/me", headers={"Authorization": "Bearer tok_1"}).status_code != 429
    assert client.get("/v1/me", headers={"Authorization": "Bearer tok_2"}).status_code != 429
    # A renewed token is the same caller: the third request is over the cap.
    assert client.get("/v1/me", headers={"Authorization": "Bearer tok_1"}).status_code == 429
    # An unknown token is not bucketed (auth answers 401).
    for _ in range(3):
        assert client.get("/v1/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_an_mcp_tool_call_counts_against_its_caller() -> None:
    settings = Settings(
        db_password="placeholder", rate_limit_enabled=True, rate_limit_rps=2,
        rate_limit_window_s=60.0,
    )
    app = create_app(settings=settings)
    caller = Principal.static("agent", admin=True)
    app.state.authenticator = StaticAuthenticator({"tok": caller})
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/v1/me", headers={"Authorization": "Bearer tok"}).status_code != 429
    with app.state.lent_tokens.lend(caller, "list_tenants") as lent:
        headers = {"Authorization": f"Bearer {lent}"}
        assert client.get("/v1/me", headers=headers).status_code != 429
        assert client.get("/v1/me", headers=headers).status_code == 429


# ── Middleware end-to-end ──────────────────────────────────────────────────


def _client(enabled: bool, rps: int, window_s: float = 60.0) -> TestClient:
    """Build an app with rate-limiting on. We pick a long window so
    we don't have to fight wall-clock during the test."""
    settings = Settings(
        db_password="placeholder",
        auth_disabled=True,
        rate_limit_enabled=enabled,
        rate_limit_rps=rps,
        rate_limit_window_s=window_s,
    )
    app = create_app(settings=settings)
    return TestClient(app, raise_server_exceptions=False)


def test_rate_limit_enforced_for_authenticated_requests() -> None:
    client = _client(enabled=True, rps=2)
    h = {"Authorization": "Bearer tok_abc_def_ghi"}

    r1 = client.get("/v1/info", headers=h)
    r2 = client.get("/v1/info", headers=h)
    r3 = client.get("/v1/info", headers=h)   # over cap

    assert r1.status_code != 429
    assert r2.status_code != 429
    assert r3.status_code == 429
    assert "retry_after_s" in r3.json()
    assert int(r3.headers["Retry-After"]) >= 1


def test_rate_limit_exempts_healthz() -> None:
    client = _client(enabled=True, rps=1)
    for _ in range(5):
        # /healthz has no auth and is on the exempt list — should never 429.
        resp = client.get("/healthz")
        assert resp.status_code == HTTPStatus.OK


def test_rate_limit_exempts_metrics() -> None:
    client = _client(enabled=True, rps=1)
    for _ in range(5):
        resp = client.get("/metrics")
        assert resp.status_code == HTTPStatus.OK


def test_rate_limit_per_token_isolation() -> None:
    """Two tokens both get their own RPS budget."""
    client = _client(enabled=True, rps=1)
    a = {"Authorization": "Bearer tok_aaa_aaa_aaa"}
    b = {"Authorization": "Bearer tok_bbb_bbb_bbb"}

    assert client.get("/v1/info", headers=a).status_code != 429
    assert client.get("/v1/info", headers=a).status_code == 429   # over for `a`
    # `b` still has its full budget:
    assert client.get("/v1/info", headers=b).status_code != 429


def test_rate_limit_off_when_disabled() -> None:
    client = _client(enabled=False, rps=1)
    h = {"Authorization": "Bearer tok_xxx_xxx_xxx"}
    for _ in range(10):
        assert client.get("/v1/info", headers=h).status_code != 429


def test_rate_limit_passes_unauthenticated_through() -> None:
    """Requests without Authorization header don't have a token to key
    on; the middleware passes them through and lets the downstream auth
    decide. (Anonymous /healthz is exempt anyway; this test covers the
    behaviour on a normally-authed route hit without a token.)"""
    client = _client(enabled=True, rps=1)
    # Hit a route that requires auth. With no token, we expect 401, not 429.
    for _ in range(5):
        resp = client.get("/v1/info")
        # Could be 200 (info doesn't require auth) or 401 — neither is 429.
        assert resp.status_code != 429

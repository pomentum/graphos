"""Per-token rate-limiter tests.

Pure-Python — uses TestClient and a synthetic clock for the bucket so
we don't actually wait one second of wall time per scenario.
"""

from __future__ import annotations

import hashlib
from http import HTTPStatus
from typing import cast

import pytest
from fastapi.testclient import TestClient

from graphos.app import create_app
from graphos.config import Settings
from graphos.observability.rate_limit import (
    _BucketRegistry,
    _token_id_from,
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


# ── token_id extraction ────────────────────────────────────────────────────


class _FakeRequest:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def _sha12(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


@pytest.mark.parametrize(
    ("auth_header", "expected"),
    [
        ("Bearer tok_abc123def456_extra", _sha12("tok_abc123def456_extra")),
        ("bearer  tok_xyz",     _sha12("tok_xyz")),        # spaces tolerated, lowercase ok
        ("Bearer ",              None),                     # empty token
        ("tok_no_scheme",        None),                     # missing scheme
        ("",                     None),
    ],
)
def test_token_id_from(auth_header, expected) -> None:
    req = cast("object", _FakeRequest({"authorization": auth_header}))
    assert _token_id_from(req) == expected     # type: ignore[arg-type]


def test_token_id_hides_the_token_and_separates_shared_prefixes() -> None:
    a = _token_id_from(cast("object", _FakeRequest({"authorization": "Bearer tok_shared_AAAA"})))
    b = _token_id_from(cast("object", _FakeRequest({"authorization": "Bearer tok_shared_BBBB"})))
    assert a != b
    assert "tok_" not in (a or "")


# ── Middleware end-to-end ──────────────────────────────────────────────────


def _client(enabled: bool, rps: int, window_s: float = 60.0) -> TestClient:
    """Build an app with rate-limiting on. We pick a long window so
    we don't have to fight wall-clock during the test."""
    settings = Settings(
        db_password="placeholder",
        auth_tokens_file=None, auth_disabled=True,
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

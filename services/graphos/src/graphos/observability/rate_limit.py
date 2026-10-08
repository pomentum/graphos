"""Per-caller sliding-window rate limiter.

Single-replica, in-memory. Each caller (the signed-in user; access tokens
are short-lived, so a bucket per token would reset at every renewal) has a
deque of recent request
timestamps; the window slides every request. If the count inside the
window exceeds the configured RPS×window cap, we return 429 with
`Retry-After` set to the time until the oldest in-window request ages
out.

Why sliding window (not token bucket):
- Bursty traffic doesn't cleanly fit a constant-rate-with-burst model.
- The "last N requests in the last M seconds" question matches the
  shape consumers actually ask ("don't overwhelm me at peak").
- The deque is O(1) push, O(1) head trim, O(1) length.

Why in-memory only:
- One graphos-api replica today. When we go multi-replica we'll need
  Redis or a shared backing store, but that's a future change.
- Not a security feature — it's an availability/fairness one.
  Determined bad actors will sidestep any per-token limit by rotating
  tokens, so we don't try to be airtight.

Exempt routes (`/healthz`, `/readyz`, `/metrics`):
- Orchestrators need these to always succeed.
- They don't carry an auth header so the per-token bucket would
  effectively rate-limit by IP, which isn't what we want here.

Configuration via Settings:
  GRAPHOS_RATE_LIMIT_ENABLED = true
  GRAPHOS_RATE_LIMIT_RPS     = 100      requests per second per token
  GRAPHOS_RATE_LIMIT_WINDOW_S = 1.0     sliding-window length
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import Counter
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger(__name__)


# Exempt routes — orchestrator-facing, no auth, never rate-limited.
_EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


# Prometheus counter so dashboards see when limits fire.
RATE_LIMITED_TOTAL = Counter(
    "graphos_rate_limited_total",
    "Number of requests rejected by the per-token rate limiter.",
    labelnames=("token_id",),     # short hash of the token, not the token itself
)


class _Bucket:
    """Sliding window of timestamps for one token. Thread-safe."""

    __slots__ = ("_lock", "_times", "_cap", "_window_s")

    def __init__(self, cap: int, window_s: float) -> None:
        self._lock = threading.Lock()
        self._times: deque[float] = deque()
        self._cap = cap
        self._window_s = window_s

    def admit(self, now: float) -> tuple[bool, float]:
        """Return ``(admitted, retry_after_s)``.

        admit=True means the request is under the cap. admit=False means
        reject; retry_after_s is the seconds until the oldest in-window
        request ages out.
        """
        with self._lock:
            # Trim everything older than the window.
            cutoff = now - self._window_s
            while self._times and self._times[0] <= cutoff:
                self._times.popleft()

            if len(self._times) < self._cap:
                self._times.append(now)
                return True, 0.0

            # At cap. Retry after the oldest request ages out.
            retry_after = self._times[0] + self._window_s - now
            return False, max(retry_after, 0.001)


class _BucketRegistry:
    """Thread-safe map of token_id → bucket. Stale buckets are
    garbage-collected lazily: a bucket whose deque is empty after trim
    can be safely deleted by any thread."""

    __slots__ = ("_lock", "_buckets", "_cap", "_window_s")

    def __init__(self, cap: int, window_s: float) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        self._cap = cap
        self._window_s = window_s

    def get(self, token_id: str) -> _Bucket:
        with self._lock:
            bucket = self._buckets.get(token_id)
            if bucket is None:
                bucket = _Bucket(self._cap, self._window_s)
                self._buckets[token_id] = bucket
        return bucket


def _short_id(value: str) -> str:
    """Short, opaque identifier of a caller (or, without auth, of the token).

    The first 12 hex chars of its SHA-256: stable, distinct across values even
    when they share a prefix, and revealing nothing when it shows up as a
    metric label or a rate-limit bucket key.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    return auth.split(" ", 1)[1].strip() or None


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,                                                # noqa: ANN001
        *,
        enabled: bool = True,
        rps: int = 100,
        window_s: float = 1.0,
    ) -> None:
        super().__init__(app)
        self._enabled = enabled
        self._registry = _BucketRegistry(cap=rps, window_s=window_s)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not self._enabled or request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        bearer = _bearer(request)
        if bearer is None:
            # Unauthenticated request — let it through; auth middleware
            # will decide. Rate-limiting by IP is a different feature
            # (different storage shape, different concerns) and we don't
            # try to do it here.
            return await call_next(request)
        authenticator = getattr(request.app.state, "authenticator", None)
        if authenticator is not None:
            principal = await authenticator.authenticate(bearer)
            if principal is None:
                # Unknown or expired token: auth answers 401.  Not bucketing
                # it keeps random bearer strings from growing the bucket map
                # or the metric's label set without bound.
                return await call_next(request)
            token_id = _short_id(principal.consumer)
        else:
            token_id = _short_id(bearer)

        bucket = self._registry.get(token_id)
        admitted, retry_after = bucket.admit(time.monotonic())
        if admitted:
            return await call_next(request)

        RATE_LIMITED_TOTAL.labels(token_id=token_id).inc()
        return JSONResponse(
            status_code=429,
            content={
                "detail": (
                    "rate limit exceeded; retry after the indicated seconds"
                ),
                "retry_after_s": round(retry_after, 3),
            },
            headers={"Retry-After": str(max(1, int(retry_after) + 1))},
        )

"""Schema-agnostic K/V cache endpoints.

PUT/GET/DELETE ``/v1/{tenant}/graph/cache/{key}``.

Two-tier read path:

* **In-process LRU** (per-replica, ``OrderedDict`` + TTL guard).  Sub-ms
  hits for hot snapshots.  Size-capped to keep memory bounded; defaults
  ~256 entries / ~30 MB depending on payload size.  Kept in step across
  replicas: a PUT or DELETE is announced (``graphos.sync``) and the other
  replicas evict the key; an entry is also served for at most
  ``GRAPHOS_CACHE_MAX_AGE_S`` seconds, so a lost announcement only delays.
* **PG-backed durable layer** (``graphos_meta.cache`` table).  Survives
  graphos-api restarts; cross-replica truth if/when graphos scales out.
  ~5-10 ms when LRU misses.

Designed for callers that compute an expensive aggregate (summary
snapshot, top-K query result, materialised drilldown) and want to
amortise the cost across consumers.  Schema-agnostic — graphos doesn't
interpret the key or value, it just stores and serves them.

Cost vs stamping aggregates onto a metadata node (e.g. ``:Team``)
via ``POST .../nodes/{external_id}/properties``:

* Node stamping requires a label that already exists in the
  schema; this surface needs no label at all.
* Node stamping is best for aggregates that ARE properties of
  a metadata node (e.g. "this project's stats live ON the
  project node").  This surface is best for aggregates that DON'T
  have a natural node anchor (cross-tenant rollups, page-level caches,
  ephemeral results).

Both can coexist — a consumer can stamp ``:Team`` AND write to this
cache, letting two reader paths read from whichever they prefer.
"""

from __future__ import annotations

import json as _json
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from graphos import sync
from graphos.api.graph import _check_tenant
from graphos.auth import ReadPrincipal, WritePrincipal

MAX_CACHE_KEY_LEN = 256

router = APIRouter(tags=["cache"], prefix="/{tenant}/graph")


# ── Wire models ────────────────────────────────────────────────────────────


class CachePutRequest(BaseModel):
    """One-shot cache write.  ``value`` is opaque JSON the server
    persists without interpretation; ``ttl_seconds`` of ``None`` means
    "no expiry" (still bounded by LRU pressure on the in-process side).
    """

    value: Any = Field(
        ...,
        description=(
            "Any JSON-serialisable value.  Stored as JSONB; round-trips "
            "preserving key order is not guaranteed.  Caller is "
            "responsible for the schema."
        ),
    )
    ttl_seconds: int | None = Field(
        default=None,
        ge=1,
        le=30 * 24 * 3600,  # 30 days cap to prevent forever-stuck rows
        description=(
            "Time-to-live in seconds from the moment graphos commits "
            "the write.  ``None`` means no expiry."
        ),
    )


class CachePutResponse(BaseModel):
    tenant: str
    key: str
    expires_at: datetime | None
    bytes_written: int
    elapsed_ms: int


class CacheGetResponse(BaseModel):
    tenant: str
    key: str
    value: Any
    expires_at: datetime | None
    cached_at: datetime
    source: str = Field(
        ..., description="``lru`` or ``pg`` — which tier served the hit.",
    )
    elapsed_ms: int


# ── In-process LRU ─────────────────────────────────────────────────────────


# Defaults tuned for a summary-snapshot workload: a few hundred entries
# of ~20 KB each ≈ 4-6 MB resident.  Adjustable via env at startup.
_DEFAULT_LRU_CAPACITY = 256


class _Entry:
    """One LRU slot — value plus the absolute deadline for TTL."""

    __slots__ = ("value", "expires_at", "cached_at", "loaded_at")

    def __init__(
        self, value: Any, expires_at: datetime | None,
        cached_at: datetime,
    ) -> None:
        self.value = value
        self.expires_at = expires_at
        self.cached_at = cached_at
        self.loaded_at = time.monotonic()   # when this replica took it in

    def is_expired(self, now: datetime, max_age_s: float) -> bool:
        if self.expires_at is not None and self.expires_at <= now:
            return True
        return time.monotonic() - self.loaded_at > max_age_s


class CacheLRU:
    """Thread-safe size-capped LRU keyed on ``(tenant, key)``.

    ``threading.Lock`` is fine here because every cache op is a single
    O(1) dict mutation — contention is bounded even under high QPS.  A
    finer-grained per-shard lock would be a premature optimisation at
    today's load.
    """

    def __init__(self, capacity: int = _DEFAULT_LRU_CAPACITY, max_age_s: float = 30.0) -> None:
        self._capacity = max(1, capacity)
        # Longest an entry is served without re-reading the table: the bound
        # on staleness when another replica's change was not announced.
        self.max_age_s = max_age_s
        self._store: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
        self._lock = Lock()

    def get(self, tenant: str, key: str) -> _Entry | None:
        now = datetime.now(UTC)
        with self._lock:
            entry = self._store.get((tenant, key))
            if entry is None:
                return None
            if entry.is_expired(now, self.max_age_s):
                # Lazy eviction — drop the slot on read so it doesn't
                # occupy capacity for entries the caller will never see.
                self._store.pop((tenant, key), None)
                return None
            # Move to MRU position.
            self._store.move_to_end((tenant, key))
            return entry

    def put(
        self, tenant: str, key: str, value: Any,
        expires_at: datetime | None,
    ) -> _Entry:
        now = datetime.now(UTC)
        entry = _Entry(value=value, expires_at=expires_at, cached_at=now)
        with self._lock:
            self._store[(tenant, key)] = entry
            self._store.move_to_end((tenant, key))
            # Evict from the LRU end until we're back under capacity.
            while len(self._store) > self._capacity:
                self._store.popitem(last=False)
        return entry

    def evict(self, tenant: str, key: str) -> bool:
        with self._lock:
            return self._store.pop((tenant, key), None) is not None

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def evict_tenant(self, tenant: str) -> int:
        """Drop every entry of ``tenant`` (its data was deleted)."""
        with self._lock:
            keys = [k for k in self._store if k[0] == tenant]
            for k in keys:
                del self._store[k]
            return len(keys)

    def size(self) -> int:
        with self._lock:
            return len(self._store)


# Module-level singleton; ``GraphosState`` wires it onto ``app.state``
# at startup so handlers reach it through ``request.app.state.cache_lru``.
LRU = CacheLRU()


def on_notification(payload: str) -> None:
    """Another replica changed a key: drop it here (``graphos.sync``)."""
    try:
        note = _json.loads(payload)
    except ValueError:
        return
    if note.get("by") == sync.INSTANCE_ID:
        return
    if note.get("key") is None:
        LRU.evict_tenant(str(note.get("tenant")))
    else:
        LRU.evict(str(note.get("tenant")), str(note.get("key")))


# ── Routes ─────────────────────────────────────────────────────────────────


@router.put(
    "/cache/{key}",
    response_model=CachePutResponse,
    summary="Upsert a JSON value into the per-tenant K/V cache",
)
async def cache_put(
    tenant: str,
    key: str,
    body: CachePutRequest,
    request: Request,
    principal: WritePrincipal,
) -> CachePutResponse:
    _check_tenant(tenant, principal)
    if not key or len(key) > MAX_CACHE_KEY_LEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"cache key must be 1..{MAX_CACHE_KEY_LEN} chars",
        )
    started = time.perf_counter()
    now = datetime.now(UTC)
    expires_at = (
        now + timedelta(seconds=body.ttl_seconds)
        if body.ttl_seconds is not None else None
    )

    # asyncpg's jsonb binding accepts a Python value if we encode it
    # explicitly — keeps the binding stable across BaseModel.value
    # being any JSON type (dict, list, scalar).
    payload = _json.dumps(body.value, ensure_ascii=False)
    bytes_written = len(payload.encode("utf-8"))

    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO graphos_meta.cache (tenant, key, value, expires_at, updated_at)
            VALUES ($1, $2, $3::jsonb, $4, now())
            ON CONFLICT (tenant, key) DO UPDATE
              SET value      = EXCLUDED.value,
                  expires_at = EXCLUDED.expires_at,
                  updated_at = now();
            """,
            tenant, key, payload, expires_at,
        )
        await sync.notify_cache(conn, tenant, key)

    # Populate the LRU so the next read (likely soon — caches are
    # written immediately before the first read) gets the warm path.
    LRU.put(tenant, key, body.value, expires_at)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return CachePutResponse(
        tenant=tenant,
        key=key,
        expires_at=expires_at,
        bytes_written=bytes_written,
        elapsed_ms=elapsed_ms,
    )


@router.get(
    "/cache/{key}",
    response_model=CacheGetResponse,
    summary="Fetch a cached value (LRU → PG fallback)",
    description=(
        "Returns the cached value if present and not expired.  The "
        "``source`` field reports which tier served the hit "
        "(``lru`` / ``pg``).  Missing or expired keys return 404; "
        "callers should treat 404 as 'recompute and PUT'."
    ),
)
async def cache_get(
    tenant: str,
    key: str,
    request: Request,
    principal: ReadPrincipal,
) -> CacheGetResponse:
    _check_tenant(tenant, principal)
    started = time.perf_counter()

    # Tier 1: in-process LRU.
    hot = LRU.get(tenant, key)
    if hot is not None:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return CacheGetResponse(
            tenant=tenant,
            key=key,
            value=hot.value,
            expires_at=hot.expires_at,
            cached_at=hot.cached_at,
            source="lru",
            elapsed_ms=elapsed_ms,
        )

    # Tier 2: durable PG row.
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT value, expires_at, updated_at
            FROM graphos_meta.cache
            WHERE tenant = $1 AND key = $2
              AND (expires_at IS NULL OR expires_at > now());
            """,
            tenant, key,
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no cache entry for tenant={tenant!r} key={key!r}",
        )

    # asyncpg returns jsonb as str — round-trip through json so the
    # ``value`` field on the response is a real Python object instead
    # of a JSON-string.  This is unavoidable: asyncpg doesn't decode
    # jsonb unless a codec is registered, and registering one globally
    # would change every other jsonb column's shape.
    raw_value = row["value"]
    value = _json.loads(raw_value) if isinstance(raw_value, str) else raw_value

    # Backfill the LRU so the next read short-circuits.
    LRU.put(tenant, key, value, row["expires_at"])

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return CacheGetResponse(
        tenant=tenant,
        key=key,
        value=value,
        expires_at=row["expires_at"],
        cached_at=row["updated_at"],
        source="pg",
        elapsed_ms=elapsed_ms,
    )


@router.delete(
    "/cache/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Invalidate a cached value (LRU + PG)",
)
async def cache_delete(
    tenant: str,
    key: str,
    request: Request,
    principal: WritePrincipal,
) -> None:
    _check_tenant(tenant, principal)
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM graphos_meta.cache WHERE tenant=$1 AND key=$2",
            tenant, key,
        )
        await sync.notify_cache(conn, tenant, key)
    LRU.evict(tenant, key)

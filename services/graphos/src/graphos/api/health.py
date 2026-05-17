"""Liveness and readiness endpoints.

- ``/healthz``: process alive. Does not touch DB.
- ``/readyz``:  asyncpg pool exists and DB roundtrip succeeds. Used by
  orchestrators (compose ``depends_on: condition: service_healthy``).

Note: readiness does **not** depend on any tenant being registered.
graphos is ready as soon as it can talk to graphos-db; tenants are
runtime-registered concerns.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response, status

from graphos.bootstrap import registered_tenants

log = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)

    if pool is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "reason": "lifespan_not_complete"}

    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1;")
    except Exception as exc:  # noqa: BLE001
        # Unauthenticated endpoint: log the cause, return only a reason code.
        log.warning("readyz: database unreachable", exc_info=exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "reason": "db_unreachable"}

    # Tenant names are not listed here: this endpoint needs no token.
    return {"status": "ready", "registered_tenants": len(registered_tenants())}

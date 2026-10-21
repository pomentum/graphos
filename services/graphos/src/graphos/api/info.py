"""Service metadata endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from graphos import __version__
from graphos.bootstrap import registered_tenants
from graphos.db import fetch_db_version
from graphos.louvain import LOUVAIN_AVAILABLE
from graphos.louvain_compute import ALGORITHMS

router = APIRouter(tags=["info"])


@router.get(
    "/info",
    summary="Service version, database versions and features",
    description=(
        "Needs no token. The graphos, PostgreSQL and Apache AGE versions, the number of "
        "registered tenants (not their names), and which optional features this server has: "
        "`louvain` (community detection), `community_algorithms` (those it runs: "
        "`louvain`, `leiden`), `metrics`, `cypher` (the Cypher console)."
    ),
)
async def info(request: Request) -> dict[str, Any]:
    pool = request.app.state.pool
    db = await fetch_db_version(pool)
    return {
        "service": "graphos",
        "version": __version__,
        "postgres_version": db["postgres_version"],
        "age_version": db["age_version"],
        # Count only: /v1/info needs no token, so tenant names are not
        # disclosed here (admins list them via GET /v1/tenants).
        "tenants": len(registered_tenants()),
        "features": {
            "louvain": LOUVAIN_AVAILABLE,
            # Community algorithms this build runs (both come with the louvain extra).
            "community_algorithms": list(ALGORITHMS) if LOUVAIN_AVAILABLE else [],
            "metrics": True,
            "cypher": getattr(request.app.state, "console", None) is not None,
        },
    }

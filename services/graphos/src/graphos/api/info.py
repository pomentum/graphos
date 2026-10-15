"""Service metadata endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from graphos import __version__
from graphos.bootstrap import registered_tenants
from graphos.db import fetch_db_version
from graphos.louvain import LOUVAIN_AVAILABLE

router = APIRouter(tags=["info"])


@router.get(
    "/info",
    summary="Service version, database versions and features",
    description=(
        "Needs no token. The graphos, PostgreSQL and Apache AGE versions, the number of "
        "registered tenants (not their names), and which optional features this server has: "
        "`louvain`, `metrics`, `cypher` (the Cypher console). `database_deprecated` says "
        "why the database is deprecated for graphos (PostgreSQL before 18, AGE before 1.8), "
        "or is null."
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
        # Why this database is deprecated for graphos, or null.
        "database_deprecated": db["deprecated"],
        # Count only: /v1/info needs no token, so tenant names are not
        # disclosed here (admins list them via GET /v1/tenants).
        "tenants": len(registered_tenants()),
        "features": {
            "louvain": LOUVAIN_AVAILABLE,
            "metrics": True,
            "cypher": getattr(request.app.state, "console", None) is not None,
        },
    }

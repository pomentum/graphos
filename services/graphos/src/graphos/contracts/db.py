"""Pydantic wire contracts for /v1/db/* operational endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TableStat(BaseModel):
    schema_name: str
    table_name: str
    n_live_tuples: int
    n_dead_tuples: int
    total_bytes: int
    index_bytes: int
    last_vacuum: str | None = None
    last_autovacuum: str | None = None
    last_analyze: str | None = None


class DbStatsResponse(BaseModel):
    postgres_version: str
    database_size_bytes: int
    age_version: str | None = None
    tables: list[TableStat] = Field(default_factory=list)


class SlowQuery(BaseModel):
    query: str
    calls: int
    total_exec_ms: float
    mean_exec_ms: float
    rows: int


class SlowQueriesResponse(BaseModel):
    available: bool = Field(
        ...,
        description="False if the pg_stat_statements extension isn't present or accessible.",
    )
    queries: list[SlowQuery] = Field(default_factory=list)
    note: str | None = None


class VacuumRequest(BaseModel):
    """Body for POST /v1/db/vacuum. All fields optional."""

    tables: list[str] | None = Field(
        default=None,
        description=(
            "If set, vacuum only these tables (`schema.table`). Otherwise "
            "vacuum the whole database (every tenant)."
        ),
    )
    full: bool = Field(
        default=False,
        description="If true, run VACUUM FULL (exclusive lock — use sparingly).",
    )
    analyze: bool = Field(default=True, description="Also run ANALYZE.")


class VacuumResponse(BaseModel):
    tables_processed: list[str] = Field(default_factory=list)
    elapsed_ms: int

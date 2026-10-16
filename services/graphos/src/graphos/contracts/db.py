"""Pydantic wire contracts for /v1/db/* operational endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TableStat(BaseModel):
    """One table's size and vacuum state (`pg_stat_user_tables`)."""

    schema_name: str = Field(..., description="The table's schema: a tenant, or graphos's own.")
    table_name: str = Field(..., description="The table's name.")
    n_live_tuples: int = Field(..., description="Estimated live rows (`n_live_tup`).")
    n_dead_tuples: int = Field(
        ..., description="Estimated dead rows not yet vacuumed (`n_dead_tup`).",
    )
    total_bytes: int = Field(
        ...,
        description="Size on disk with indexes and TOAST (`pg_total_relation_size`), in bytes.",
    )
    index_bytes: int = Field(
        ..., description="Size of the table's indexes (`pg_indexes_size`), in bytes.",
    )
    last_vacuum: str | None = Field(
        None, description="When it was last vacuumed by hand (ISO 8601); null if never.",
    )
    last_autovacuum: str | None = Field(
        None, description="When autovacuum last vacuumed it (ISO 8601); null if never.",
    )
    last_analyze: str | None = Field(
        None, description="When it was last analyzed by hand (ISO 8601); null if never.",
    )


class DbStatsResponse(BaseModel):
    """The database's versions, size and largest tables."""

    postgres_version: str = Field(..., description="The PostgreSQL server version.")
    database_size_bytes: int = Field(..., description="The database's size on disk, in bytes.")
    age_version: str | None = Field(
        None, description="The Apache AGE extension's version; null when it is not installed.",
    )
    tables: list[TableStat] = Field(
        default_factory=list,
        description="The 200 largest tables, by `total_bytes` descending.",
    )


class SlowQuery(BaseModel):
    """One statement's totals in `pg_stat_statements`."""

    query: str = Field(
        ..., description="The normalised statement text (constants replaced by `$n`).",
    )
    calls: int = Field(..., description="Times it ran.")
    total_exec_ms: float = Field(..., description="Total execution time, in milliseconds.")
    mean_exec_ms: float = Field(..., description="Mean execution time, in milliseconds.")
    rows: int = Field(..., description="Rows retrieved or affected, over all calls.")


class SlowQueriesResponse(BaseModel):
    """The slowest statements on average, from `pg_stat_statements`."""

    available: bool = Field(
        ...,
        description="False if the pg_stat_statements extension isn't present or accessible.",
    )
    queries: list[SlowQuery] = Field(
        default_factory=list,
        description="At most `limit` statements, by mean execution time descending.",
    )
    note: str | None = Field(
        None, description="Why `available` is false, and how to enable it; null otherwise.",
    )


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
    """What a vacuum processed."""

    tables_processed: list[str] = Field(
        default_factory=list,
        description="The `tables` asked for, as sent; empty when the whole database was vacuumed.",
    )
    elapsed_ms: int = Field(..., description="Time the vacuum took, in milliseconds.")

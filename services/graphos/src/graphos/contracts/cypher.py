"""Bodies of the Cypher console (``graphos.api.cypher``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_QUERY_CHARS = 20_000
SAVED_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$"
PARAM_NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"


class CypherRequest(BaseModel):
    """``POST /v1/{tenant}/cypher``: a read-only Cypher query on the tenant's graph."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS)
    params: dict[str, Any] = Field(
        default_factory=dict, description="Parameters, used in the query as `$name`.",
    )
    limit: int | None = Field(
        None, ge=1, le=100_000,
        description="Rows to return at most (the server's `GRAPHOS_CYPHER_MAX_ROWS` caps it).",
    )


class CypherResult(BaseModel):
    """What a query returned: a table, and the graph its vertices and edges make."""

    columns: list[str] = Field(
        description="Named after the RETURN items (`AS` aliases, else the expression).",
    )
    rows: list[list[Any]] = Field(
        description=(
            "One list of cells per row. A vertex is `{\"node\": {id, label, properties}}`, an "
            "edge `{\"edge\": {type, properties, src_id, src_label, dst_id, dst_label}}` "
            "(ends null when not returned), a path a list of them."
        ),
    )
    graph: dict[str, list[dict[str, Any]]] = Field(
        description="`nodes` returned, and `edges` whose two ends are among them.",
    )
    truncated: bool = Field(description="More rows matched than were returned.")
    elapsed_ms: int


class SavedQueryParam(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "number", "boolean", "list"] = "string"
    label: str | None = Field(None, max_length=200, description="Shown in the form.")


class SavedQueryBody(BaseModel):
    """``PUT /v1/{tenant}/cypher/saved/{name}``."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field("", max_length=2_000)
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS)
    params: dict[str, SavedQueryParam] = Field(default_factory=dict)
    show: Literal["table", "graph"] = "table"


class SavedQuery(SavedQueryBody):
    name: str
    created_by: str | None = None
    updated_at: datetime | None = None


class SavedQueryRun(BaseModel):
    """``POST /v1/{tenant}/cypher/saved/{name}:run``."""

    model_config = ConfigDict(extra="forbid")

    params: dict[str, Any] = Field(default_factory=dict)
    limit: int | None = Field(None, ge=1, le=100_000)

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

    query: str = Field(
        ..., min_length=1, max_length=MAX_QUERY_CHARS,
        description="The Cypher text, at most 20000 characters; it must only read.",
    )
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
    elapsed_ms: int = Field(..., description="Time the query took, in milliseconds.")


class SavedQueryParam(BaseModel):
    """A parameter a saved query declares: its type, and a label for the UI's form."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "number", "boolean", "list"] = Field(
        "string",
        description=(
            "The JSON type its value must have when the query is run: `string`, `number` "
            "(not a boolean), `boolean` or `list`; another type answers 422."
        ),
    )
    label: str | None = Field(None, max_length=200, description="Shown in the form.")


class SavedQueryBody(BaseModel):
    """``PUT /v1/{tenant}/cypher/saved/{name}``."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field("", max_length=2_000, description="What the query is for.")
    query: str = Field(
        ..., min_length=1, max_length=MAX_QUERY_CHARS,
        description=(
            "The Cypher text, at most 20000 characters; not run or checked until it is run."
        ),
    )
    params: dict[str, SavedQueryParam] = Field(
        default_factory=dict,
        description="The parameters it takes, by name (used in the query as `$name`).",
    )
    show: Literal["table", "graph"] = Field(
        "table", description="How the UI shows its result: as a `table` or as a `graph`.",
    )


class SavedQuery(SavedQueryBody):
    """A saved, named Cypher query of a tenant."""

    name: str = Field(..., description="Its name, unique in the tenant.")
    created_by: str | None = Field(
        None, description="The consumer that saved it last (a replace changes it).",
    )
    updated_at: datetime | None = Field(None, description="When it was last saved.")


class SavedQueryRun(BaseModel):
    """``POST /v1/{tenant}/cypher/saved/{name}:run``."""

    model_config = ConfigDict(extra="forbid")

    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "A value for every parameter the saved query declares, of its type, and no other "
            "(else 422)."
        ),
    )
    limit: int | None = Field(
        None, ge=1, le=100_000,
        description="Rows to return at most (the server's `GRAPHOS_CYPHER_MAX_ROWS` caps it).",
    )

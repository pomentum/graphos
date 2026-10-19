"""Wire contracts for ``GET /v1/{tenant}/schema/summary`` (ADR-0002 §1)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ValueCount(BaseModel):
    """One value a property takes, and on how many rows."""

    value: str = Field(..., description=(
        "The value as text: a string as it is, any other value as JSON (`5`, `true`, `null`). "
        "One longer than 80 characters is cut and ends with `…`."
    ))
    kind: str | None = Field(None, description=(
        "The value's JSON type: `string`, `number`, `boolean`, `null`, `array` or `object`."
    ))
    count: int = Field(..., description="Rows that hold it (estimated when the property is).")


class SummaryProperty(BaseModel):
    """One property of a label: declared, observed, or both."""

    name: str = Field(..., description="The property name, exactly as stored.")
    type: str | None = Field(None, description="The declared type; null when not declared.")
    required: bool = Field(False, description="Declared required.")
    enum: list[object] | None = Field(None, description="The declared allowed values, if any.")
    records: int = Field(..., description="Rows of the label that have it.")
    distinct: int = Field(..., description="Distinct values among them.")
    values: list[ValueCount] | None = Field(
        None,
        description=(
            "Every value with its count, most frequent first, when there are at most 20; "
            "null otherwise."
        ),
    )
    estimated: bool = Field(False, description="Counted on a sample of a large label.")


class SummaryLabel(BaseModel):
    """One vertex label with its nodes and properties."""

    label: str = Field(..., description="The label.")
    count: int = Field(..., description="Nodes stored under this label itself.")
    parent: str | None = Field(None, description="Its parent label (`label_hierarchy`), if any.")
    properties: list[SummaryProperty] = Field(
        default_factory=list, description="Its properties, the most held first.",
    )
    properties_total: int = Field(
        ..., description="How many properties it has; more than listed when a cap cut them.",
    )


class SummaryLink(BaseModel):
    """One (source label, edge type, target label) with its edge count."""

    src_label: str = Field(..., description="The sources' label.")
    type: str = Field(..., description="The edge type.")
    dst_label: str = Field(..., description="The targets' label.")
    count: int = Field(..., description="Edges, estimated on a sample of a large type.")


class SummarySearchIndex(BaseModel):
    """One search index declared for the tenant."""

    name: str = Field(..., description="The index name, for `graph/search?index=`.")
    labels: list[str] = Field(..., description="The labels it covers (`*`: all).")
    properties: list[str] = Field(..., description="The properties it searches.")
    analyzer: str = Field(..., description="`identifier` or `text`.")


class SummarySavedQuery(BaseModel):
    """One saved Cypher query and its parameters."""

    name: str = Field(..., description="Its name, for `cypher/saved/{name}:run`.")
    description: str = Field("", description="What it is for.")
    params: dict[str, str] = Field(
        default_factory=dict, description="Each parameter's name and type.",
    )


class SchemaSummaryResponse(BaseModel):
    """The tenant's schema as a model needs it before querying: compact and measured."""

    tenant: str = Field(..., description="The tenant.")
    labels: list[SummaryLabel] = Field(..., description="Labels with nodes, the largest first.")
    labels_total: int = Field(..., description="Labels with nodes; more than listed when cut.")
    links: list[SummaryLink] = Field(..., description="Label pairs per edge type, largest first.")
    links_total: int = Field(..., description="Label pairs with edges; more than listed when cut.")
    search_indexes: list[SummarySearchIndex] = Field(
        default_factory=list, description="The declared search indexes.",
    )
    saved_queries: list[SummarySavedQuery] = Field(
        default_factory=list, description="The saved queries, with their parameters.",
    )
    computed: list[str] = Field(
        default_factory=list,
        description="Properties graphos computes and writes itself (read-only to consumers).",
    )
    estimated: bool = Field(..., description="Some counts come from samples.")
    text: str = Field(..., description="The same summary as compact text, for a prompt.")
    cached: bool = Field(..., description="Served from the per-tenant cache.")
    elapsed_ms: int = Field(..., description="Time spent answering, in milliseconds.")


SummaryFormat = Literal["json", "text"]

"""Wire contracts for a tenant's property schema: the properties each vertex
label and edge type carries, and how strictly writes are held to them.

Declared in a registration as ``property_schema`` (see ``graphos.properties``
for how the definitions are combined and checked).  Every model forbids
unknown keys, so a misspelt keyword is refused rather than ignored.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from graphos import reserved

# The same patterns as the rest of a registration (``contracts.tenants``).
_LABEL = re.compile(r"^[A-Z][A-Za-z0-9_]{0,62}$")
_PROPERTY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# Every label (or every edge type), now and later: as for search indexes.
ALL = "*"

Mode = Literal["off", "warn", "strict"]
Undeclared = Literal["allow", "warn", "reject"]
PropertyType = Literal["string", "number", "integer", "boolean", "array", "object", "any"]
ItemType = Literal["string", "number", "integer", "boolean", "any"]

MAX_PROPERTIES = 200
MAX_ENUM = 1000
MAX_PATTERN = 200
MAX_DESCRIPTION = 1000

Scalar = str | int | float | bool


def _compiles(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"pattern {pattern!r} does not compile: {exc}") from None
    return pattern


def _enum_fits(values: list[Scalar], kind: str) -> None:
    """Enum values of the property's type (``any``: any scalars)."""
    for value in values:
        ok = {
            "string": isinstance(value, str),
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        }.get(kind, True)
        if not ok:
            raise ValueError(f"enum value {value!r} is not of type {kind}")


class ItemSpec(BaseModel):
    """What every item of an ``array`` property must be."""

    model_config = ConfigDict(extra="forbid")

    type: ItemType = "any"
    enum: list[Scalar] | None = Field(None, min_length=1, max_length=MAX_ENUM)
    pattern: str | None = Field(None, max_length=MAX_PATTERN)

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, v: str | None) -> str | None:
        return v if v is None else _compiles(v)

    @model_validator(mode="after")
    def _consistent(self) -> ItemSpec:
        if self.enum is not None:
            _enum_fits(self.enum, self.type)
        if self.pattern is not None and self.type not in ("string", "any"):
            raise ValueError("pattern applies to string items only")
        return self


class PropertySpec(BaseModel):
    """One declared property: its type and what its value must be."""

    model_config = ConfigDict(extra="forbid")

    type: PropertyType = Field(
        "any", description="JSON type: `true` is not a number, `1.0` not an integer.",
    )
    required: bool = Field(False, description="Absent or `null` is a violation.")
    recommended: bool = Field(
        False, description="Absent or `null` is counted as a warning only, in any mode.",
    )
    enum: list[Scalar] | None = Field(None, min_length=1, max_length=MAX_ENUM)
    pattern: str | None = Field(
        None, max_length=MAX_PATTERN, description="A full match, on strings (Python `re`).",
    )
    format: Literal["date", "date-time"] | None = Field(
        None, description="ISO 8601; a `date-time` needs an offset or `Z`.",
    )
    minimum: float | None = Field(None, description="Inclusive, on numbers.")
    maximum: float | None = Field(None, description="Inclusive, on numbers.")
    items: ItemSpec | None = Field(None, description="For an `array`: what every item must be.")
    description: str | None = Field(None, max_length=MAX_DESCRIPTION)

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, v: str | None) -> str | None:
        return v if v is None else _compiles(v)

    @model_validator(mode="after")
    def _consistent(self) -> PropertySpec:
        t = self.type
        if self.required and self.recommended:
            raise ValueError("a property is required or recommended, not both")
        if self.enum is not None:
            if t in ("array", "object"):
                raise ValueError(f"enum does not apply to type {t}")
            _enum_fits(self.enum, t)
        if self.pattern is not None and t not in ("string", "any"):
            raise ValueError(f"pattern applies to strings, not type {t}")
        if self.format is not None and t not in ("string", "any"):
            raise ValueError(f"format applies to strings, not type {t}")
        if (self.minimum is not None or self.maximum is not None) and t not in (
            "number", "integer", "any",
        ):
            raise ValueError(f"minimum and maximum apply to numbers, not type {t}")
        if (
            self.minimum is not None and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("minimum is greater than maximum")
        if self.items is not None and t != "array":
            raise ValueError("items applies to type array only")
        return self


class LabelPropertiesSpec(BaseModel):
    """The properties of one vertex label or edge type (or of every one: ``*``)."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(
        None, max_length=MAX_DESCRIPTION,
        description="What the label or type stands for; documentation only, never checked.",
    )
    mode: Mode | None = Field(
        None, description="Overrides the tenant's mode for this label or type.",
    )
    undeclared: Undeclared | None = Field(
        None,
        description="Keys this entry does not declare: `allow` (the default), `warn` or `reject`.",
    )
    properties: dict[str, PropertySpec] = Field(default_factory=dict)

    @field_validator("properties")
    @classmethod
    def _names(cls, v: dict[str, PropertySpec]) -> dict[str, PropertySpec]:
        if len(v) > MAX_PROPERTIES:
            raise ValueError(f"at most {MAX_PROPERTIES} properties per label or type")
        for name, spec in v.items():
            if not _PROPERTY.fullmatch(name):
                raise ValueError(f"property {name!r} must match {_PROPERTY.pattern}")
            if reserved.is_reserved(name):
                raise ValueError(f"property {name!r} is written by graphos and cannot be declared")
            id_extras = spec.model_dump(exclude_defaults=True, exclude={"pattern", "description"})
            if name == "id" and id_extras:
                raise ValueError("`id` may declare a `pattern` (and a description) only")
        return v


class PropertySchemaSpec(BaseModel):
    """``property_schema`` in a registration.

    ``mode``: `off` (stored, shown and exported, nothing checked), `warn`
    (checked, written anyway, counted) or `strict` (violating rows refused);
    omitted, it stays as registered (`off` for a new tenant).  `vertex` and
    `edge` map a label or type (or ``*``) to its entry; an entry declared
    again replaces the stored one, and ``null`` removes it.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Mode | None = None
    vertex: dict[str, LabelPropertiesSpec | None] = Field(default_factory=dict)
    edge: dict[str, LabelPropertiesSpec | None] = Field(default_factory=dict)

    @field_validator("vertex", "edge")
    @classmethod
    def _entries(cls, v: dict[str, Any]) -> dict[str, Any]:
        for name, entry in v.items():
            if name != ALL and not _LABEL.fullmatch(name):
                raise ValueError(
                    f"property_schema entry {name!r} must be `*` or match {_LABEL.pattern}"
                )
            if name == ALL and entry is not None and entry.description is not None:
                raise ValueError("`*` is not a label: it takes no description")
        return v


# ── The effective definitions ────────────────────────────────────────────────


class EffectiveProperty(BaseModel):
    """One property as a write of the label or type is checked against it."""

    source: str = Field(
        description="The entry the definition comes from last: `*`, a label or a type.",
    )
    definition: dict[str, Any] = Field(
        description="The keywords combined from `*`, the ancestors and the entry itself.",
    )


class EffectivePolicy(BaseModel):
    """How one vertex label's or edge type's writes are checked."""

    mode: Mode
    undeclared: Undeclared
    undeclared_source: str
    properties: dict[str, EffectiveProperty] = Field(default_factory=dict)


class PropertyDefinitionsResponse(BaseModel):
    """``GET /v1/{tenant}/graph/properties``: every label's and type's definitions."""

    tenant: str
    mode: Mode = Field(description="The tenant's mode; an entry may override it.")
    vertex: dict[str, EffectivePolicy] = Field(default_factory=dict)
    edge: dict[str, EffectivePolicy] = Field(default_factory=dict)


# ── The report on stored data ────────────────────────────────────────────────

MAX_EXAMPLES = 100


class PropertiesCheckRequest(BaseModel):
    """What ``properties:check`` reads: every label and edge type by default."""

    model_config = ConfigDict(extra="forbid")

    labels: list[str] | None = Field(None, description="Vertex labels to check; all when omitted.")
    types: list[str] | None = Field(None, description="Edge types to check; all when omitted.")
    examples: int = Field(
        10, ge=0, le=MAX_EXAMPLES, description="Example ids kept per rule broken.",
    )


class LabelCheck(BaseModel):
    """What one vertex label's or edge type's stored rows break."""

    kind: Literal["vertex", "edge"]
    name: str
    rows: int = Field(description="Rows read (a node under its own label only).")
    rows_with_errors: int = Field(description="Rows strict would refuse.")
    rows_with_warnings: int
    errors: dict[str, int] = Field(
        default_factory=dict, description="By `<label or *>.<property>:<rule>`.",
    )
    warnings: dict[str, int] = Field(default_factory=dict)
    examples: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Per rule broken, some rows: a node's id, an edge as `src_id -> dst_id`.",
    )
    undeclared: dict[str, int] = Field(
        default_factory=dict,
        description="Property names the rows carry that no definition names, with how many rows.",
    )


class PropertiesCheckResponse(BaseModel):
    tenant: str
    mode: Mode = Field(description="The tenant's mode; the check reads as if every rule applied.")
    rows: int
    rows_with_errors: int
    rows_with_warnings: int
    labels: list[LabelCheck]
    elapsed_ms: int

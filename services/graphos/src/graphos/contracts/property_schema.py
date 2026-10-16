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

    type: ItemType = Field(
        "any",
        description=(
            "Each item's JSON type: `string`, `number`, `integer`, `boolean` or `any` (the "
            "default)."
        ),
    )
    enum: list[Scalar] | None = Field(
        None, min_length=1, max_length=MAX_ENUM,
        description="The values an item may take (1 to 1000, of the item type).",
    )
    pattern: str | None = Field(
        None, max_length=MAX_PATTERN,
        description="A full match on string items (Python `re`); `string` or `any` items only.",
    )

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
    enum: list[Scalar] | None = Field(
        None, min_length=1, max_length=MAX_ENUM,
        description=(
            "The values allowed (1 to 1000, of the property's type); not for `array` or `object`."
        ),
    )
    pattern: str | None = Field(
        None, max_length=MAX_PATTERN, description="A full match, on strings (Python `re`).",
    )
    format: Literal["date", "date-time"] | None = Field(
        None, description="ISO 8601; a `date-time` needs an offset or `Z`.",
    )
    minimum: float | None = Field(None, description="Inclusive, on numbers.")
    maximum: float | None = Field(None, description="Inclusive, on numbers.")
    items: ItemSpec | None = Field(None, description="For an `array`: what every item must be.")
    description: str | None = Field(
        None, max_length=MAX_DESCRIPTION,
        description="What the property holds; documentation only, never checked.",
    )

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
    properties: dict[str, PropertySpec] = Field(
        default_factory=dict,
        description=(
            "The declared properties by name, at most 200. `id` may declare only a `pattern` "
            "(and a description); `graphos_*` and `community_id` cannot be declared."
        ),
    )

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

    mode: Mode | None = Field(
        None,
        description=(
            "`off` (nothing checked), `warn` (checked, written anyway, counted) or `strict` "
            "(violating rows refused); omitted, it stays as registered."
        ),
    )
    vertex: dict[str, LabelPropertiesSpec | None] = Field(
        default_factory=dict,
        description="Entries by vertex label, or `*` for every label; `null` removes one.",
    )
    edge: dict[str, LabelPropertiesSpec | None] = Field(
        default_factory=dict,
        description="Entries by edge type, or `*` for every type; `null` removes one.",
    )

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

    mode: Mode = Field(
        ...,
        description=(
            "The mode its writes are checked in: `off`, `warn` or `strict` (the tenant's, unless "
            "an entry overrides it)."
        ),
    )
    undeclared: Undeclared = Field(
        ...,
        description="What happens to keys no definition names: `allow`, `warn` or `reject`.",
    )
    undeclared_source: str = Field(
        ...,
        description="The entry `undeclared` comes from: `*`, a label or a type (`*` by default).",
    )
    properties: dict[str, EffectiveProperty] = Field(
        default_factory=dict, description="Each declared property's combined definition, by name.",
    )


class PropertyDefinitionsResponse(BaseModel):
    """``GET /v1/{tenant}/graph/properties``: every label's and type's definitions."""

    tenant: str = Field(..., description="The tenant.")
    mode: Mode = Field(description="The tenant's mode; an entry may override it.")
    vertex: dict[str, EffectivePolicy] = Field(
        default_factory=dict, description="Every registered vertex label's policy, by label.",
    )
    edge: dict[str, EffectivePolicy] = Field(
        default_factory=dict, description="Every registered edge type's policy, by type.",
    )


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

    kind: Literal["vertex", "edge"] = Field(
        ..., description="`vertex` for a vertex label, `edge` for an edge type.",
    )
    name: str = Field(..., description="The vertex label or edge type.")
    rows: int = Field(description="Rows read (a node under its own label only).")
    rows_with_errors: int = Field(description="Rows strict would refuse.")
    rows_with_warnings: int = Field(
        ...,
        description=(
            "Rows with at least one warning (a `recommended` property absent, an undeclared "
            "key under `warn`)."
        ),
    )
    errors: dict[str, int] = Field(
        default_factory=dict, description="By `<label or *>.<property>:<rule>`.",
    )
    warnings: dict[str, int] = Field(
        default_factory=dict,
        description="Warnings by `<label or *>.<property>:<rule>`, most frequent first.",
    )
    examples: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Per rule broken, some rows: a node's id, an edge as `src_id -> dst_id`.",
    )
    undeclared: dict[str, int] = Field(
        default_factory=dict,
        description="Property names the rows carry that no definition names, with how many rows.",
    )


class PropertiesCheckResponse(BaseModel):
    """What the stored rows break of the property schema; nothing is written."""

    tenant: str = Field(..., description="The tenant.")
    mode: Mode = Field(description="The tenant's mode; the check reads as if every rule applied.")
    rows: int = Field(..., description="Rows read, over every label and type checked.")
    rows_with_errors: int = Field(..., description="Rows strict would refuse, over all of them.")
    rows_with_warnings: int = Field(
        ..., description="Rows with at least one warning, over all of them.",
    )
    labels: list[LabelCheck] = Field(
        ..., description="One entry per vertex label, then per edge type, checked.",
    )
    elapsed_ms: int = Field(..., description="Time the check took, in milliseconds.")

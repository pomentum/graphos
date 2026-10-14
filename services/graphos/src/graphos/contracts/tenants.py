"""Pydantic wire contracts for tenant registration."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Tenant name = AGE graph name = postgres schema name. AGE preserves
# case (the schema is created as a quoted identifier), so mixed case
# is fine end-to-end. Constrain to ASCII alphanumerics + underscore
# so the value can flow through ``create_graph('<name>')`` and
# ``ALTER TABLE "<name>"."..."`` without escaping concerns.
_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
_LABEL_PATTERN = re.compile(r"^[A-Z][A-Za-z0-9_]{0,62}$")

# Closed DSL keys allowed inside an edge label's ``resolve_rule`` block.
# Anything outside this set causes the registration call to reject — the
# DSL is intentionally closed (no user-supplied SQL, no escape hatches).
# See ``graphos/resolver/compiler.py`` for the compilation contract.
_RESOLVE_RULE_KEYS: frozenset[str] = frozenset({
    "src_has_array",
    "src_array_of_objects",
    "src_match_field",
    "dst_value_from",
    "match_op",
    "require_different",
    "require_same",
    # Destination-side restriction by AGE label (child-table name).
    # List of label-name strings; only candidate destination labels in
    # this set produce SQL arms.  Same closed-DSL discipline as the
    # other keys — every value is matched against
    # ``_LABEL_PATTERN`` so it can flow into ``"<tenant>"."<label>"``
    # interpolation without escaping concerns.
    "require_dst_label",
    # Destination-side restriction by property value(s).  Dict of
    # ``<property_name>: [<allowed_value>, ...]``.  Property names go
    # through ``_PROP_IDENT_PATTERN``; values are parameterised
    # (``$N::text[]``) so they're injection-safe and the spec author
    # picks an arbitrary set of allowed strings.  Combined with
    # ``require_dst_label``, this lets one closed primitive express
    # "destination must be a Document node WHERE kind is one of
    # {guide, reference}" — enough to route an edge type to the right
    # kind of target instead of any node that happens to share the
    # matched value.
    "require_dst_property",
})
_RESOLVE_RULE_MATCH_OPS: frozenset[str] = frozenset({"eq", "eq_ci", "eq_norm"})
# Property names embedded in a rule MUST be plain identifiers so they can
# flow into a quoted JSONB property accessor (``properties ->> '<name>'``)
# without escaping concerns.  The compiler re-checks this on every
# compilation, but rejecting at registration time gives the operator a
# clearer error message.
_PROP_IDENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_resolve_rule(rule_owner: str, rule: dict[str, Any]) -> None:
    """Validate the shape of a single ``resolve_rule`` block.

    ``rule_owner`` is the edge-label name; it shows up in the error
    message so an operator can pinpoint which rule is malformed.

    Closed-DSL validation only — does NOT inspect what any property name
    *means*.  graphos stays domain-agnostic.
    """
    unknown = set(rule.keys()) - _RESOLVE_RULE_KEYS
    if unknown:
        raise ValueError(
            f"edge label {rule_owner!r}: resolve_rule has unknown key(s) "
            f"{sorted(unknown)!r}; allowed: {sorted(_RESOLVE_RULE_KEYS)!r}"
        )

    has_arr = rule.get("src_has_array")
    has_obj = rule.get("src_array_of_objects")
    if has_arr is None and has_obj is None:
        raise ValueError(
            f"edge label {rule_owner!r}: resolve_rule must set exactly "
            "one of 'src_has_array' or 'src_array_of_objects'"
        )
    if has_arr is not None and has_obj is not None:
        raise ValueError(
            f"edge label {rule_owner!r}: resolve_rule may set only one of "
            "'src_has_array' or 'src_array_of_objects' (not both)"
        )

    if has_obj is not None and not rule.get("src_match_field"):
        raise ValueError(
            f"edge label {rule_owner!r}: 'src_array_of_objects' requires "
            "'src_match_field' to be set"
        )
    if has_arr is not None and rule.get("src_match_field") is not None:
        raise ValueError(
            f"edge label {rule_owner!r}: 'src_match_field' applies only "
            "with 'src_array_of_objects', not 'src_has_array'"
        )

    if not rule.get("dst_value_from"):
        raise ValueError(
            f"edge label {rule_owner!r}: resolve_rule requires "
            "'dst_value_from'"
        )

    # Property-name strings must be plain identifiers; reject anything
    # that could not be a JSONB key accessed via ``properties ->> '<x>'``.
    for key in ("src_has_array", "src_array_of_objects", "src_match_field"):
        val = rule.get(key)
        if val is not None and not _PROP_IDENT_PATTERN.fullmatch(str(val)):
            raise ValueError(
                f"edge label {rule_owner!r}: resolve_rule {key!r}={val!r} "
                f"must match {_PROP_IDENT_PATTERN.pattern}"
            )

    match_op = rule.get("match_op", "eq")
    if match_op not in _RESOLVE_RULE_MATCH_OPS:
        raise ValueError(
            f"edge label {rule_owner!r}: resolve_rule match_op={match_op!r} "
            f"must be one of {sorted(_RESOLVE_RULE_MATCH_OPS)!r}"
        )

    for key in ("require_different", "require_same"):
        seq = rule.get(key)
        if seq is None:
            continue
        if not isinstance(seq, list):
            raise ValueError(
                f"edge label {rule_owner!r}: resolve_rule {key!r} must be "
                "a list of property-name strings"
            )
        for name in seq:
            if not isinstance(name, str) or not _PROP_IDENT_PATTERN.fullmatch(name):
                raise ValueError(
                    f"edge label {rule_owner!r}: resolve_rule {key!r} "
                    f"contains invalid property name {name!r}"
                )

    # ``require_dst_label`` — list of allowed destination AGE labels.
    # Each entry must match _LABEL_PATTERN (same constraint applied to
    # any label that flows into "<tenant>"."<label>" interpolation).
    dst_labels = rule.get("require_dst_label")
    if dst_labels is not None:
        if not isinstance(dst_labels, list) or not dst_labels:
            raise ValueError(
                f"edge label {rule_owner!r}: resolve_rule "
                "'require_dst_label' must be a non-empty list of "
                "label-name strings"
            )
        for lbl in dst_labels:
            if not isinstance(lbl, str) or not _LABEL_PATTERN.fullmatch(lbl):
                raise ValueError(
                    f"edge label {rule_owner!r}: resolve_rule "
                    f"'require_dst_label' contains invalid label "
                    f"{lbl!r} (must match {_LABEL_PATTERN.pattern})"
                )

    # ``require_dst_property`` — dict of property name → list of
    # allowed string values.  Property names must match the ident
    # pattern (they flow into JSONB ``->> '<name>'`` accessors); the
    # values are parameterised at compile time so any non-control
    # string is acceptable.
    dst_props = rule.get("require_dst_property")
    if dst_props is not None:
        if not isinstance(dst_props, dict) or not dst_props:
            raise ValueError(
                f"edge label {rule_owner!r}: resolve_rule "
                "'require_dst_property' must be a non-empty dict "
                "{property_name: [allowed_values, ...]}"
            )
        for prop_name, allowed in dst_props.items():
            if not isinstance(prop_name, str) or not _PROP_IDENT_PATTERN.fullmatch(prop_name):
                raise ValueError(
                    f"edge label {rule_owner!r}: resolve_rule "
                    f"'require_dst_property' key {prop_name!r} must match "
                    f"{_PROP_IDENT_PATTERN.pattern}"
                )
            if not isinstance(allowed, list) or not allowed:
                raise ValueError(
                    f"edge label {rule_owner!r}: resolve_rule "
                    f"'require_dst_property'[{prop_name!r}] must be a "
                    "non-empty list of allowed values"
                )
            for v in allowed:
                if not isinstance(v, str):
                    raise ValueError(
                        f"edge label {rule_owner!r}: resolve_rule "
                        f"'require_dst_property'[{prop_name!r}] contains "
                        f"non-string value {v!r}"
                    )

    # ``dst_value_from`` is a tiny expression DSL ("concat(a, '.', b)" or
    # a bare identifier).  Full lexical validation lives in the compiler;
    # here we just demand a non-empty string.
    if not isinstance(rule["dst_value_from"], str) or not rule["dst_value_from"].strip():
        raise ValueError(
            f"edge label {rule_owner!r}: resolve_rule 'dst_value_from' "
            "must be a non-empty string"
        )


_SEARCH_INDEX_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_SEARCH_PROPERTY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_TS_LANGUAGE = re.compile(r"^[a-z_]{1,63}$")


class SearchIndexSpec(BaseModel):
    """A named search index (``graphos.search``): which labels and
    properties, and how their text is analysed."""

    labels: list[str] = Field(
        default_factory=lambda: ["*"], min_length=1,
        description='Vertex labels covered; `["*"]` (default) for every label, now and later.',
    )
    properties: list[str] = Field(
        default_factory=lambda: ["name"], min_length=1, max_length=8,
        description="String properties searched; a node scores by its best-matching one.",
    )
    analyzer: Literal["identifier", "text"] = Field(
        "identifier",
        description=(
            "`identifier`: names, ids, paths: case-insensitive, camelCase and acronyms "
            "split, punctuation as word breaks, typo-tolerant. `text`: prose: Postgres "
            "full-text search with stemming for `language`."
        ),
    )
    language: str = Field(
        "simple",
        description="`text` analyzer: a text-search configuration, e.g. `english`.",
    )

    @field_validator("labels")
    @classmethod
    def _labels(cls, v: list[str]) -> list[str]:
        if "*" in v and len(v) > 1:
            raise ValueError('use either ["*"] or a list of labels, not both')
        for lab in v:
            if lab != "*" and not _LABEL_PATTERN.fullmatch(lab):
                raise ValueError(f"label {lab!r} must match {_LABEL_PATTERN.pattern}")
        return v

    @field_validator("properties")
    @classmethod
    def _properties(cls, v: list[str]) -> list[str]:
        for p in v:
            if not _SEARCH_PROPERTY.fullmatch(p):
                raise ValueError(f"property {p!r} must match {_SEARCH_PROPERTY.pattern}")
        if len(set(v)) != len(v):
            raise ValueError("properties must not repeat")
        return v

    @field_validator("language")
    @classmethod
    def _language(cls, v: str) -> str:
        if not _TS_LANGUAGE.fullmatch(v):
            raise ValueError(f"language {v!r} must match {_TS_LANGUAGE.pattern}")
        return v


class EdgeLabelSpec(BaseModel):
    """One entry in ``edge_labels`` — name plus optional resolve rule.

    Back-compat: the registration body's ``edge_labels`` field also
    accepts a list of bare strings (each string is treated as
    ``{name: "<str>", resolve_rule: None}``).  See
    ``TenantSpecBody._coerce_edge_labels``.
    """

    name: str
    resolve_rule: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _name_safe(cls, v: str) -> str:
        if not _LABEL_PATTERN.fullmatch(v):
            raise ValueError(
                f"edge label {v!r} must match {_LABEL_PATTERN.pattern} (UpperCamelCase)"
            )
        return v

    @model_validator(mode="after")
    def _validate_rule(self) -> EdgeLabelSpec:
        if self.resolve_rule is not None:
            _validate_resolve_rule(self.name, self.resolve_rule)
        return self


# A registration may declare at most this many label pairs in one request.
MAX_EDGE_ENDPOINTS = 5000

_INDEX_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_COLUMN_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
# Columns graphos adds to label tables itself; a generated column may not take
# these names (or any other ``graphos_`` name).
_RESERVED_COLUMNS = frozenset(
    {"id", "properties", "start_id", "end_id", "end_community_id", "start_community_id"},
)
_RESERVED_COLUMN_PREFIX = "graphos_"
# Before 0.6.0 ``indexes`` and ``generated_columns`` were SQL statements.
_SQL_REPLACED = (
    "SQL statements are no longer accepted (since 0.6.0): declare {what} instead, "
    "and graphos writes the SQL"
)


def _property_name(v: str) -> str:
    if not _SEARCH_PROPERTY.fullmatch(v):
        raise ValueError(f"property {v!r} must match {_SEARCH_PROPERTY.pattern}")
    return v


class PropertyIndexSpec(BaseModel):
    """An index on one or more properties of a vertex label.

    graphos writes the index with the expression its own reads use, so it
    serves them: ``equality`` (a btree) the ``where`` filters of
    ``nodes:scan`` and the other reads that take them (``=``, ``!=``, ``in``,
    ``exists``), ``trigram`` the substring match of ``/graph/search`` without
    an ``index``. Built without blocking writes.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(description="A vertex label of the tenant.")
    properties: list[str] = Field(
        min_length=1, max_length=4,
        description="The properties indexed, in this order (one for `trigram`).",
    )
    kind: Literal["equality", "trigram"] = Field(
        "equality",
        description=(
            "`equality`: `where` filters (=, !=, in, exists); best for short values such as "
            "ids, keys and kinds. `trigram`: substring search on one text property."
        ),
    )
    unique: bool = Field(
        False,
        description=(
            "No two nodes of the label may share the values (`equality` only). A write that "
            "would is refused with 409; registering fails with 422 if nodes already do."
        ),
    )
    name: str | None = Field(
        None,
        description=(
            "Postgres index name; omitted, graphos names it `<Label>_<properties>_<kind>_idx`. "
            "An index of that name that already exists is kept as it is."
        ),
    )

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        if not _LABEL_PATTERN.fullmatch(v):
            raise ValueError(f"label {v!r} must match {_LABEL_PATTERN.pattern}")
        return v

    @field_validator("properties")
    @classmethod
    def _properties(cls, v: list[str]) -> list[str]:
        for p in v:
            _property_name(p)
        if len(set(v)) != len(v):
            raise ValueError("properties must not repeat")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v is not None and not _INDEX_NAME.fullmatch(v):
            raise ValueError(f"index name {v!r} must match {_INDEX_NAME.pattern}")
        return v

    @model_validator(mode="after")
    def _kind_rules(self) -> PropertyIndexSpec:
        if self.kind == "trigram" and (len(self.properties) != 1 or self.unique):
            raise ValueError("a trigram index covers exactly one property and is not unique")
        return self


class GeneratedColumnSpec(BaseModel):
    """A stored column on a vertex label, computed from one property.

    For reading the label table with SQL directly (reports, BI tools);
    graphos's own reads do not use it. graphos writes the expression, which
    never fails a write: a value of another JSON type gives ``NULL``. Adding
    one to a label that already has nodes rewrites its table.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(description="A vertex label of the tenant.")
    column: str = Field(description="The column name (lower case).")
    property: str = Field(description="The property it holds.")
    type: Literal["text", "numeric", "boolean", "jsonb"] = Field(
        "text",
        description=(
            "`text`: the value as text (any type); `numeric`, `boolean`: the value when it is "
            "a JSON number / boolean, else NULL; `jsonb`: the value as JSON."
        ),
    )

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        if not _LABEL_PATTERN.fullmatch(v):
            raise ValueError(f"label {v!r} must match {_LABEL_PATTERN.pattern}")
        return v

    @field_validator("column")
    @classmethod
    def _column(cls, v: str) -> str:
        if not _COLUMN_NAME.fullmatch(v):
            raise ValueError(f"column {v!r} must match {_COLUMN_NAME.pattern}")
        if v in _RESERVED_COLUMNS or v.startswith(_RESERVED_COLUMN_PREFIX):
            raise ValueError(f"column name {v!r} is reserved by graphos")
        return v

    @field_validator("property")
    @classmethod
    def _property(cls, v: str) -> str:
        return _property_name(v)


class EdgeEndpointSpec(BaseModel):
    """One label pair an edge type may join: ``src`` -> ``dst``.

    Accepts ``{"src": "File", "dst": "Function"}`` or ``["File", "Function"]``.
    A child label (``label_hierarchy``) satisfies a pair declared for its parent.
    """

    src: str
    dst: str

    @model_validator(mode="before")
    @classmethod
    def _from_pair(cls, v: Any) -> Any:
        if isinstance(v, (list, tuple)):
            if len(v) != 2:  # noqa: PLR2004 - a pair
                raise ValueError("an endpoint pair is [src_label, dst_label]")
            return {"src": v[0], "dst": v[1]}
        return v

    @field_validator("src", "dst")
    @classmethod
    def _label_safe(cls, v: str) -> str:
        if not _LABEL_PATTERN.fullmatch(v):
            raise ValueError(f"label '{v}' must match {_LABEL_PATTERN.pattern} (UpperCamelCase)")
        return v


class TenantSpecBody(BaseModel):
    """Body for POST /v1/tenants.

    Mirrors ``graphos.bootstrap.TenantSpec`` over the wire. Validation rules:
    - ``name`` is a postgres-safe identifier: a letter, then letters, digits
      or underscores (max 63).
    - Vertex / edge labels follow openCypher convention (UpperCamelCase).
    - ``edge_labels`` accepts both plain string entries and the richer
      ``EdgeLabelSpec`` shape ``{name, resolve_rule}``.  When
      ``resolve_rule`` is set the entry becomes a schema-driven property-
      match edge resolver (see ``graphos/resolver/`` and ``POST
      /v1/{tenant}/graph/edges:resolve``).
    - ``property_indexes`` and ``generated_columns`` are declarations, not
      SQL: graphos writes the DDL itself (``graphos.declared``). A request
      that still sends SQL (a non-empty ``indexes``, or a string in
      ``generated_columns``) is refused; the empty lists older clients send
      are accepted.
    - ``edge_endpoints`` declares, per edge type, the label pairs it may
      join (``{"CALLS": [{"src": "Function", "dst": "Function"}]}``): the
      schema's associations. Merged by union; checked against the tenant's
      registered and incoming labels. With ``strict_endpoints`` on, a write
      of a declared type outside its pairs is refused; types with no
      declared pairs stay open.
    - ``label_hierarchy`` declares per-label postgres-inheritance edges
      ``{child: parent}``. After ``create_vlabel`` runs, graphos wires
      ``ALTER TABLE <name>."<child>" INHERIT <name>."<parent>"`` so a
      single-label write to the child is automatically visible to
      ``MATCH (n:<parent>)`` queries (AGE doesn't support multi-label
      syntax, but postgres table inheritance provides equivalent
      semantics — verified end-to-end against AGE 1.6).  Both child and
      parent must appear in ``vertex_labels``.  Acyclic; chains
      (grandparent → parent → child) are permitted but flatly listed.
    """

    name: str = Field(..., min_length=1, max_length=63)
    vertex_labels: list[str] = Field(default_factory=list)
    edge_labels: list[EdgeLabelSpec] = Field(default_factory=list)
    property_indexes: list[PropertyIndexSpec] = Field(
        default_factory=list,
        description="Indexes on vertex-label properties; added to the registered ones.",
    )
    generated_columns: list[GeneratedColumnSpec] = Field(
        default_factory=list,
        description="Stored columns computed from a property; added to the registered ones.",
    )
    label_hierarchy: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of child→parent label pairs. graphos sets up postgres "
            "table inheritance so writes under the child label are "
            "visible to MATCH on the parent. Both labels must be "
            "declared in `vertex_labels`."
        ),
    )
    meta: dict[str, str | None] = Field(
        default_factory=dict,
        description=(
            "Free-form notes about the tenant, as strings. A registration adds and replaces "
            "keys; `null` removes one (`{\"model\": null}`). Replacing the schema "
            "(`force_shrink`) keeps only the keys sent."
        ),
    )
    search_indexes: dict[str, SearchIndexSpec] = Field(
        default_factory=dict,
        description=(
            "Named search indexes, queried with `GET …/graph/search?index=<name>`. "
            "Declaring a name again replaces its definition."
        ),
    )

    edge_endpoints: dict[str, list[EdgeEndpointSpec]] = Field(
        default_factory=dict,
        description=(
            "Per edge type, the (source label, target label) pairs it may join. "
            "Added to what is registered (never removed by a merge; "
            "`DELETE /v1/tenants/{name}/edge_endpoints` removes one). A child "
            "label satisfies a pair declared for its parent."
        ),
    )
    strict_endpoints: bool | None = Field(
        default=None,
        description=(
            "Refuse edge writes of a declared type outside its declared pairs "
            "(`true`), or accept them (`false`, the default for a new tenant). "
            "Omitted: unchanged."
        ),
    )

    @field_validator("edge_endpoints")
    @classmethod
    def _endpoints_safe(
        cls, v: dict[str, list[EdgeEndpointSpec]],
    ) -> dict[str, list[EdgeEndpointSpec]]:
        total = 0
        for edge_type, pairs in v.items():
            if not _LABEL_PATTERN.fullmatch(edge_type):
                raise ValueError(
                    f"edge_endpoints type '{edge_type}' must match {_LABEL_PATTERN.pattern}"
                )
            total += len(pairs)
        if total > MAX_EDGE_ENDPOINTS:
            raise ValueError(f"at most {MAX_EDGE_ENDPOINTS} edge_endpoints pairs per registration")
        # Duplicates within a type are harmless; keep each pair once.
        return {
            t: list({(p.src, p.dst): p for p in pairs}.values()) for t, pairs in v.items()
        }

    @field_validator("search_indexes")
    @classmethod
    def _search_index_names(cls, v: dict[str, SearchIndexSpec]) -> dict[str, SearchIndexSpec]:
        for name in v:
            if not _SEARCH_INDEX_NAME.fullmatch(name):
                raise ValueError(
                    f"search index name {name!r} must match {_SEARCH_INDEX_NAME.pattern}"
                )
        return v

    @field_validator("name")
    @classmethod
    def _name_safe(cls, v: str) -> str:
        if not _NAME_PATTERN.fullmatch(v):
            raise ValueError(
                f"tenant name must match {_NAME_PATTERN.pattern} "
                "(ASCII letters / digits / underscore; must start with a letter)"
            )
        return v

    @field_validator("vertex_labels")
    @classmethod
    def _vlabels_safe(cls, v: list[str]) -> list[str]:
        for label in v:
            if not _LABEL_PATTERN.fullmatch(label):
                raise ValueError(
                    f"label '{label}' must match {_LABEL_PATTERN.pattern} (UpperCamelCase)"
                )
        return v

    @field_validator("edge_labels", mode="before")
    @classmethod
    def _coerce_edge_labels(cls, v: Any) -> list[Any]:
        """Accept both plain ``["LABEL_A", "LABEL_B"]`` and the richer
        ``[{"name": "LABEL_A", "resolve_rule": {...}}]`` shape.

        Strings get promoted to the dict shape so downstream code sees a
        single representation.
        """
        if not isinstance(v, list):
            return v  # let pydantic raise its own type error
        out: list[Any] = []
        for entry in v:
            if isinstance(entry, str):
                out.append({"name": entry, "resolve_rule": None})
            else:
                out.append(entry)
        return out

    @field_validator("label_hierarchy")
    @classmethod
    def _hierarchy_labels_safe(cls, v: dict[str, str]) -> dict[str, str]:
        # Each side of the hierarchy edge must be a valid label name.
        # The cross-check against ``vertex_labels`` happens in
        # bootstrap.py (we don't have access to the sibling field
        # easily from a field_validator without a model_validator).
        for child, parent in v.items():
            for lab in (child, parent):
                if not _LABEL_PATTERN.fullmatch(lab):
                    raise ValueError(
                        f"label_hierarchy entry '{lab}' must match "
                        f"{_LABEL_PATTERN.pattern} (UpperCamelCase)"
                    )
            if child == parent:
                raise ValueError(
                    f"label_hierarchy entry {child!r} -> {parent!r}: "
                    "child and parent must differ (no self-loops)"
                )
        return v

    @model_validator(mode="before")
    @classmethod
    def _schema_file(cls, data: Any) -> Any:
        # An exported schema (graphos.schema_document) is a registration body
        # with an ``_export`` header: checked here, otherwise ignored.
        if isinstance(data, dict) and "_export" in data:
            from graphos.schema_document import check_header  # noqa: PLC0415 — no cycle at import

            check_header(data["_export"])
            data = {k: v for k, v in data.items() if k != "_export"}
        return data

    @model_validator(mode="before")
    @classmethod
    def _no_sql(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Clients before 0.6.0 always send ``"indexes": []``: an empty list
            # carries no SQL, so it is accepted (and ignored).
            if data.get("indexes"):
                raise ValueError(
                    "`indexes`: " + _SQL_REPLACED.format(
                        what='`property_indexes` ([{"label": ..., "properties": [...]}])',
                    ),
                )
            if any(isinstance(c, str) for c in data.get("generated_columns") or ()):
                raise ValueError(
                    "`generated_columns`: " + _SQL_REPLACED.format(
                        what='each as {"label", "column", "property", "type"}',
                    ),
                )
        return data

    @model_validator(mode="after")
    def _one_definition_per_column(self) -> TenantSpecBody:
        seen: dict[tuple[str, str], GeneratedColumnSpec] = {}
        for col in self.generated_columns:
            other = seen.setdefault((col.label, col.column), col)
            if other != col:
                raise ValueError(
                    f"generated column {col.label}.{col.column} is declared twice, differently"
                )
        return self


class TenantInfo(BaseModel):
    """Returned by GET /v1/tenants."""

    name: str
    vertex_labels: list[str] = Field(default_factory=list)
    edge_labels: list[str] = Field(default_factory=list)
    property_indexes: list[PropertyIndexSpec] = Field(default_factory=list)
    generated_columns: list[GeneratedColumnSpec] = Field(default_factory=list)
    n_resolve_rules: int = 0
    label_hierarchy: dict[str, str] = Field(default_factory=dict)
    meta: dict[str, str] = Field(default_factory=dict)
    search_indexes: dict[str, SearchIndexSpec] = Field(default_factory=dict)


class MeResponse(BaseModel):
    """Returned by GET /v1/me: who is signed in and what they may use."""

    consumer: str = Field(description="The user name; audit, changes and jobs record it.")
    kind: Literal["person", "service", "anonymous"] = "person"
    via: Literal["password", "oidc", "none"] = Field(
        "password",
        description="How the caller signed in: a graphos user, or the identity provider.",
    )
    admin: bool
    must_change: bool = Field(
        False, description="The password must be changed before anything else.",
    )
    tenants: list[str] = Field(
        default_factory=list,
        description="Registered tenants the caller may use (every tenant for an admin).",
    )
    levels: dict[str, Literal["read", "write", "maintain", "owner"]] = Field(
        default_factory=dict, description="The caller's level on each of `tenants`.",
    )
    create_tenants: list[str] = Field(
        default_factory=list,
        description="Patterns of tenant names the caller may create (`*` for an admin).",
    )
    preferences: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "What the caller chose in the web UI (`theme`, `last_tenant`); changed with "
            "`PATCH /v1/me/preferences`."
        ),
    )


class TenantSchema(BaseModel):
    """Returned by GET /v1/{tenant}/schema: the tenant's shape, for any token on it."""

    name: str
    vertex_labels: list[str] = Field(default_factory=list)
    edge_labels: list[str] = Field(default_factory=list)
    label_hierarchy: dict[str, str] = Field(
        default_factory=dict, description="child label -> parent label",
    )
    search_indexes: dict[str, SearchIndexSpec] = Field(default_factory=dict)
    rule_edge_labels: list[str] = Field(
        default_factory=list, description="Edge labels derived by a resolve rule.",
    )
    edge_endpoints: dict[str, list[EdgeEndpointSpec]] = Field(
        default_factory=dict,
        description="Per edge type, the label pairs it is declared to join.",
    )
    strict_endpoints: bool = Field(
        default=False,
        description="Edge writes of a declared type outside its declared pairs are refused.",
    )


class ResolveEdgesRequest(BaseModel):
    """Body for ``POST /v1/{tenant}/graph/edges:resolve``.

    Schema-driven: domain knowledge stays on the registered tenant
    spec.  The caller passes edge-type *names* only — no rule
    payload, no property names, no filter values.  Omitting
    ``edge_types`` resolves every edge type that has a registered
    ``resolve_rule`` for this tenant.
    """

    edge_types: list[str] | None = Field(
        default=None,
        description=(
            "Edge-type names to resolve.  Each must already be "
            "registered with a `resolve_rule` on the tenant spec.  "
            "Omit (or pass null) to resolve all registered types."
        ),
    )

    @field_validator("edge_types")
    @classmethod
    def _types_safe(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        for t in v:
            if not _LABEL_PATTERN.fullmatch(t):
                raise ValueError(
                    f"edge_type {t!r} must match {_LABEL_PATTERN.pattern}"
                )
        return v


class ResolveEdgesResult(BaseModel):
    """One per-edge-type entry in ``ResolveEdgesResponse.results``."""

    edge_type: str
    edges_written: int
    duration_ms: int
    warning: str | None = None


class ResolveEdgesResponse(BaseModel):
    """Aggregated result of ``POST /v1/{tenant}/graph/edges:resolve``."""

    tenant: str
    results: list[ResolveEdgesResult]
    total_edges_written: int
    total_duration_ms: int


class RegisterDryRunResponse(BaseModel):
    """``POST /v1/tenants?dry_run=true``: what the registration would change."""

    tenant: str
    exists: bool = Field(description="The tenant is registered already.")
    restores: bool = Field(
        False,
        description=(
            "The tenant is unregistered with its data kept: registering it restores it "
            "first, with the schema it had."
        ),
    )
    mode: Literal["merge", "replace"]
    changes: dict[str, Any] | None = Field(
        description=(
            "What the change feed would record: `added` / `removed` per part, "
            "`strict_endpoints` as [before, after], `registered` for a new tenant; "
            "null when nothing would change."
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "What applying it would cost (a table rewrite, a long build) or could still fail on."
        ),
    )
    status: str = "dry_run"


class RegisterResponse(BaseModel):
    tenant: str
    vertex_labels: list[str]
    edge_labels: list[str]
    n_property_indexes: int
    n_generated_columns: int
    n_inheritance_edges: int = 0
    n_resolve_rules: int = 0
    # Count of functional indexes graphos auto-derived from this tenant's
    # ``resolve_rule`` blocks and ensured on the candidate vertex tables.
    # Idempotent across registrations; ``0`` either means no rules were
    # registered or every supporting index already existed and re-creation
    # was a no-op.
    n_resolver_indexes: int = 0
    n_edge_endpoints: int = 0
    strict_endpoints: bool = False
    status: str = "bootstrapped"


class UnregisteredTenant(BaseModel):
    """A tenant whose data is kept without a registration."""

    name: str
    kind: Literal["unregistered", "orphan"] = Field(
        description=(
            "`unregistered`: its registration is kept and `POST /v1/tenants/{name}/restore` "
            "brings it back; `orphan`: a graph unregistered before graphos kept "
            "registrations (0.8.2), which only an admin may register again or delete."
        ),
    )
    vertex_labels: int = Field(0, description="Vertex labels in its kept registration.")
    edge_labels: int = Field(0, description="Edge labels in its kept registration.")
    created_at: datetime | None = None
    unregistered_at: datetime | None = None
    unregistered_by: str | None = None


class TenantList(BaseModel):
    """Returned by GET /v1/tenants."""

    tenants: list[TenantInfo]
    unregistered: list[UnregisteredTenant] | None = Field(
        None,
        description=(
            "With `?unregistered=true`: tenants unregistered with their data kept that the "
            "caller owns (every one, orphans included, for an admin)."
        ),
    )

"""Compile a tenant-registered ``resolve_rule`` into parameterised SQL.

The DSL is closed.  The compiler exposes two entry points:

* ``compile_resolve_rule`` (per-pair) produces a list of ``CompiledRule``
  objects — one per (src_label, dst_label) candidate pair inferred from
  the per-tenant property-name → labels catalogue.  Kept for tests and
  for any caller that wants per-pair introspection.
* ``compile_resolve_rules_for_edge_type`` collapses ALL rules for a
  single ``edge_type`` (and every (src_label, dst_label) candidate of
  each) into a SINGLE ``INSERT … SELECT … FROM (UNION ALL …) ON
  CONFLICT (start_id, end_id) DO NOTHING RETURNING 1`` statement.  This
  is what the ``/edges:resolve`` endpoint executes — one DB round-trip
  per edge_type instead of N×M.

Domain-agnostic by construction: the compiler emits SQL using:

* Identifiers (tenant, edge_type, vertex_label, property_name) that
  have all been validated against the strict ``_IDENT`` pattern.
* Bound parameters for every value that originated from caller data
  (string literals inside ``dst_value_from`` concat expressions).

Anything that fails the identifier check causes ``CompileError`` — the
caller is expected to surface it as an HTTP 400 on the resolve endpoint
and an HTTP 422 on the registration endpoint.

The AGE on-disk edge table schema is ``(id, start_id, end_id,
properties)`` with a UNIQUE index on ``(start_id, end_id)`` installed
by ``bootstrap.bootstrap_tenant``.  We INSERT directly against that
table — no Cypher round-trip, no agtype boxing per row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from graphos.resolver._normalize import canon_text_sql
from graphos.resolver.catalogue import PropertyLabelCatalogue

# Identifier regex applied to every identifier compiled into SQL.
# Match the same shape ``contracts.tenants`` validates at registration
# time so a rule that survived registration can never trip this guard
# accidentally.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ``dst_value_from`` tokeniser.  The expression is either
#   <ident>                      -- single property name
#   concat(<arg>, <arg>, ...)    -- chain
# where each <arg> is either an identifier (property name on dst) or
# a single-quoted string literal (becomes a bound parameter).
_TOK = re.compile(
    r"\s*("
    r"concat\(|"
    r",|"
    r"\)|"
    r"'(?:[^'\\]|\\.)*'|"   # single-quoted string, with escaped quotes
    r"[A-Za-z_][A-Za-z0-9_]*"
    r")\s*",
)


class CompileError(ValueError):
    """Raised when a resolve_rule cannot be compiled to safe SQL."""


@dataclass(frozen=True)
class CompiledRule:
    """One executable INSERT for one (src_label, dst_label) candidate.

    ``sql`` is a parameterised statement.  ``params`` is the list of
    bind parameters in $1, $2, ... order — every value that originated
    from caller data goes through bind parameters; nothing about the
    rule data is interpolated as a literal.
    """

    sql: str
    params: list[object]
    src_label: str
    dst_label: str


@dataclass(frozen=True)
class _Token:
    kind: str   # "concat_open", "comma", "close", "string", "ident"
    text: str   # the original token text (without surrounding quotes
                # for "string"); used by the compiler to emit identifiers
                # or bind-parameter slots.


def _check_ident(label: str, what: str) -> str:
    """Refuse anything that can't be a SQL identifier.  Returns the
    input unchanged on success so callers can chain.
    """
    if not isinstance(label, str) or not _IDENT.fullmatch(label):
        raise CompileError(
            f"{what} {label!r} is not a safe identifier "
            f"(must match {_IDENT.pattern})"
        )
    return label


def _tokenise_dst_value_from(expr: str) -> list[_Token]:
    """Lex ``dst_value_from`` into a token stream.

    Permitted shapes:
        IDENT
        concat(arg, arg, ...)   where arg ∈ {IDENT, 'string literal'}

    Any other token (operators, function names other than ``concat``,
    nested parens, unquoted non-identifier characters) raises
    ``CompileError``.
    """
    out: list[_Token] = []
    pos = 0
    while pos < len(expr):
        # Skip leading whitespace before matching so the trailing
        # whitespace in the previous match's consumption already
        # advanced us.  Use ``match`` anchored at pos.
        m = _TOK.match(expr, pos)
        if m is None:
            raise CompileError(
                f"dst_value_from {expr!r}: unparseable at character "
                f"{pos} (rest: {expr[pos:pos + 16]!r})"
            )
        tok = m.group(1)
        if tok == "concat(":
            out.append(_Token(kind="concat_open", text=tok))
        elif tok == ",":
            out.append(_Token(kind="comma", text=tok))
        elif tok == ")":
            out.append(_Token(kind="close", text=tok))
        elif tok.startswith("'") and tok.endswith("'"):
            # Strip the surrounding quotes; unescape any backslash-escaped
            # single quotes inside (the lex regex allows them).  The
            # resulting Python string becomes a bound parameter — it is
            # NEVER interpolated into the SQL.
            inner = tok[1:-1].replace("\\'", "'")
            out.append(_Token(kind="string", text=inner))
        else:
            # Must be a bare identifier — re-validate against the strict
            # pattern (the lex regex is intentionally a touch more
            # permissive than ``_IDENT`` so we can produce a clearer
            # error message here).
            _check_ident(tok, "dst_value_from identifier")
            out.append(_Token(kind="ident", text=tok))
        pos = m.end()
    if not out:
        raise CompileError(f"dst_value_from {expr!r}: empty expression")
    return out


@dataclass
class _ParsedDstExpr:
    """Parsed form of ``dst_value_from``: an ordered list of pieces.

    Each piece is either ``("prop", "<name>")`` — a JSONB property
    on the dst vertex — or ``("lit", "<literal>")`` — a string literal
    that compiles to a bound parameter.
    """

    pieces: list[tuple[str, str]]

    @property
    def property_names(self) -> list[str]:
        return [name for kind, name in self.pieces if kind == "prop"]


def _parse_dst_value_from(expr: str) -> _ParsedDstExpr:
    """Turn a tokenised ``dst_value_from`` into a list of pieces."""
    tokens = _tokenise_dst_value_from(expr)
    # Single identifier — the simplest, most common shape.
    if len(tokens) == 1 and tokens[0].kind == "ident":
        return _ParsedDstExpr(pieces=[("prop", tokens[0].text)])
    # ``concat(arg, arg, ...)``.
    if tokens[0].kind != "concat_open":
        raise CompileError(
            f"dst_value_from {expr!r}: must be either a bare identifier or "
            "a concat(...) call"
        )
    if tokens[-1].kind != "close":
        raise CompileError(
            f"dst_value_from {expr!r}: unterminated concat(...) call"
        )
    body = tokens[1:-1]
    if not body:
        raise CompileError(f"dst_value_from {expr!r}: empty concat(...)")
    pieces: list[tuple[str, str]] = []
    expecting_arg = True
    for tok in body:
        if expecting_arg:
            if tok.kind == "ident":
                pieces.append(("prop", tok.text))
            elif tok.kind == "string":
                pieces.append(("lit", tok.text))
            else:
                raise CompileError(
                    f"dst_value_from {expr!r}: expected identifier or "
                    f"string literal, got {tok.text!r}"
                )
            expecting_arg = False
        else:
            if tok.kind != "comma":
                raise CompileError(
                    f"dst_value_from {expr!r}: expected ',' between args, "
                    f"got {tok.text!r}"
                )
            expecting_arg = True
    if expecting_arg:
        raise CompileError(
            f"dst_value_from {expr!r}: trailing ',' inside concat(...)"
        )
    return _ParsedDstExpr(pieces=pieces)


# Helper SQL fragments.  We treat ``properties`` as AGE ``agtype``;
# casting through text → jsonb gives us first-class JSONB operators
# (``->>``, ``jsonb_array_elements_text``, ``jsonb_array_elements``).
# The cast is cheap relative to the rest of the join — empirically
# under the per-label GIN(properties) index it doesn't change plan
# shape for the property-access predicates.
def _props_text_jsonb(alias: str) -> str:
    """SQL expression: ``alias.properties`` as ``jsonb``."""
    return f"({alias}.properties::text)::jsonb"


def _prop_lookup_text(alias: str, prop: str) -> str:
    """SQL expression: text value of ``alias.properties -> '<prop>'``.

    ``prop`` is validated by ``_check_ident``; safe to interpolate.
    """
    return f"({_props_text_jsonb(alias)} ->> '{prop}')"


def _prop_lookup_array(alias: str, prop: str) -> str:
    """SQL expression: ``alias.properties -> '<prop>'`` as JSONB
    (preserved as JSONB for array unnesting)."""
    return f"({_props_text_jsonb(alias)} -> '{prop}')"


@dataclass(frozen=True)
class _NormalisedRule:
    """Internal mid-stage: validated + parsed but not yet emitted."""

    edge_type: str
    src_array_prop: str
    src_match_field: str | None     # None when src_has_array (scalars)
    dst_expr: _ParsedDstExpr
    match_op: str                   # "eq" | "eq_ci" | "eq_norm"
    require_different: tuple[str, ...]
    require_same: tuple[str, ...]
    # Destination-side restrictors (see ``contracts.tenants``
    # ``_RESOLVE_RULE_KEYS``).  Empty tuples / dict when the rule
    # doesn't constrain.
    require_dst_label: tuple[str, ...]
    require_dst_property: tuple[tuple[str, tuple[str, ...]], ...]


def _normalise(edge_type: str, rule: dict[str, object]) -> _NormalisedRule:
    """Validate + parse the rule against the closed DSL.

    Duplicates the registration-time checks for safety: a compile call
    that was reached without registration (e.g. tests) must still
    reject anything outside the DSL.
    """
    _check_ident(edge_type, "edge_type")

    has_arr = rule.get("src_has_array")
    has_obj = rule.get("src_array_of_objects")
    if (has_arr is None) == (has_obj is None):
        raise CompileError(
            f"edge_type {edge_type!r}: resolve_rule must set exactly one of "
            "'src_has_array' / 'src_array_of_objects'"
        )
    if has_arr is not None:
        src_array_prop = _check_ident(str(has_arr), "src_has_array")
        src_match_field: str | None = None
    else:
        src_array_prop = _check_ident(str(has_obj), "src_array_of_objects")
        match_field = rule.get("src_match_field")
        if not match_field:
            raise CompileError(
                f"edge_type {edge_type!r}: 'src_array_of_objects' requires "
                "'src_match_field'"
            )
        src_match_field = _check_ident(str(match_field), "src_match_field")

    expr = rule.get("dst_value_from")
    if not isinstance(expr, str) or not expr.strip():
        raise CompileError(
            f"edge_type {edge_type!r}: resolve_rule requires non-empty "
            "'dst_value_from'"
        )
    dst_expr = _parse_dst_value_from(expr)

    match_op = str(rule.get("match_op", "eq"))
    if match_op not in {"eq", "eq_ci", "eq_norm"}:
        raise CompileError(
            f"edge_type {edge_type!r}: match_op {match_op!r} must be "
            "'eq', 'eq_ci', or 'eq_norm'"
        )

    diff_raw = rule.get("require_different") or []
    same_raw = rule.get("require_same") or []
    if not isinstance(diff_raw, list) or not isinstance(same_raw, list):
        raise CompileError(
            f"edge_type {edge_type!r}: 'require_different'/'require_same' "
            "must be lists"
        )
    require_different = tuple(
        _check_ident(str(p), "require_different") for p in diff_raw
    )
    require_same = tuple(
        _check_ident(str(p), "require_same") for p in same_raw
    )

    # ── require_dst_label ──────────────────────────────────────────
    dst_labels_raw = rule.get("require_dst_label")
    if dst_labels_raw is None:
        require_dst_label: tuple[str, ...] = ()
    else:
        if not isinstance(dst_labels_raw, list) or not dst_labels_raw:
            raise CompileError(
                f"edge_type {edge_type!r}: 'require_dst_label' must be a "
                "non-empty list of label-name strings"
            )
        require_dst_label = tuple(
            _check_ident(str(lbl), "require_dst_label")
            for lbl in dst_labels_raw
        )

    # ── require_dst_property ───────────────────────────────────────
    dst_props_raw = rule.get("require_dst_property")
    if dst_props_raw is None:
        require_dst_property: tuple[tuple[str, tuple[str, ...]], ...] = ()
    else:
        if not isinstance(dst_props_raw, dict) or not dst_props_raw:
            raise CompileError(
                f"edge_type {edge_type!r}: 'require_dst_property' must be "
                "a non-empty dict {property_name: [allowed_values, ...]}"
            )
        prop_pairs: list[tuple[str, tuple[str, ...]]] = []
        for prop_name, allowed in dst_props_raw.items():
            ident = _check_ident(str(prop_name), "require_dst_property")
            if not isinstance(allowed, list) or not allowed:
                raise CompileError(
                    f"edge_type {edge_type!r}: "
                    f"'require_dst_property'[{prop_name!r}] must be a "
                    "non-empty list of allowed string values"
                )
            for v in allowed:
                if not isinstance(v, str):
                    raise CompileError(
                        f"edge_type {edge_type!r}: "
                        f"'require_dst_property'[{prop_name!r}] contains "
                        f"non-string value {v!r}"
                    )
            prop_pairs.append((ident, tuple(allowed)))
        require_dst_property = tuple(prop_pairs)

    return _NormalisedRule(
        edge_type=edge_type,
        src_array_prop=src_array_prop,
        src_match_field=src_match_field,
        dst_expr=dst_expr,
        match_op=match_op,
        require_different=require_different,
        require_same=require_same,
        require_dst_label=require_dst_label,
        require_dst_property=require_dst_property,
    )


def rule_property_names(edge_type: str, rule: dict[str, object]) -> set[str]:
    """Every node property a rule reads at the top level of a row: the
    source array, the ``dst_value_from`` properties, the ``require_*``
    properties and the ``require_dst_property`` keys.  The catalogue is
    looked up for exactly these.

    Raises ``CompileError`` for a rule outside the DSL.
    """
    norm = _normalise(edge_type, rule)
    return {
        norm.src_array_prop,
        *norm.dst_expr.property_names,
        *norm.require_different,
        *norm.require_same,
        *(prop for prop, _ in norm.require_dst_property),
    }


def _infer_labels(
    norm: _NormalisedRule,
    catalogue: PropertyLabelCatalogue,
) -> tuple[list[str], list[str]]:
    """Pick src + dst vertex-label candidates from the catalogue.

    * src candidates: every label that carries ``src_array_prop`` plus
      every property in ``require_different``/``require_same`` (the rule
      can't fire on a node that lacks the array).
    * dst candidates: every label that carries every property referenced
      by ``dst_value_from`` plus every property in
      ``require_different``/``require_same``.

    When no candidate is found for a side, returns an empty list — the
    caller is expected to record a zero-emit result and move on, per
    the design-doc spec ("rule registers successfully even when it
    references a property no node carries yet").
    """
    # Required-on-src properties: the array property + any endpoint-
    # property compared via require_*.
    src_required = {norm.src_array_prop}
    src_required.update(norm.require_different)
    src_required.update(norm.require_same)
    # Required-on-dst properties: every property referenced by
    # dst_value_from + endpoint properties.
    dst_required = set(norm.dst_expr.property_names)
    dst_required.update(norm.require_different)
    dst_required.update(norm.require_same)

    def _candidates(needed: set[str]) -> list[str]:
        if not needed:
            return []
        sets = [catalogue.labels_for(p) for p in needed]
        # Intersect — a candidate must carry every required property
        # somewhere in its row population.  This is a coarse filter
        # (we don't know that the SAME row carries all of them), but
        # it's a safe over-approximation: the SQL WHERE clauses will
        # filter out non-matching rows.
        if any(len(s) == 0 for s in sets):
            return []
        intersection = sets[0]
        for s in sets[1:]:
            intersection = intersection & s
        return sorted(intersection)

    src_candidates = _candidates(src_required)
    dst_candidates = _candidates(dst_required)

    # ``require_dst_label`` intersects the discovered candidates with
    # an explicit allow-list — short-circuits emitting SQL arms
    # against labels that lawyer-up against the join key but can't
    # legally be the destination of this edge type.  Cuts emit
    # count drastically when the join key (e.g. ``name``) is shared
    # across many labels but the edge type is meant for one of them.
    if norm.require_dst_label:
        allow = set(norm.require_dst_label)
        dst_candidates = [lbl for lbl in dst_candidates if lbl in allow]

    return src_candidates, dst_candidates


def _emit_arm(
    *,
    tenant: str,
    norm: _NormalisedRule,
    src_label: str,
    dst_label: str,
    params: list[object],
) -> str:
    """Emit one inner ``SELECT … FROM … WHERE …`` arm for a single
    (src_label, dst_label) candidate pair.

    ``params`` is a shared list — string-literal bind values get
    appended to it and the SQL refers to them as ``$<N>::text`` using
    the resulting 1-based index.  Caller is responsible for the outer
    ``INSERT INTO … (…) SELECT … FROM ( <arms joined by UNION ALL> )
    ON CONFLICT … RETURNING 1`` wrapper.
    """
    _check_ident(tenant, "tenant")
    _check_ident(src_label, "src_label")
    _check_ident(dst_label, "dst_label")

    # ── src-side unnest expression ────────────────────────────────────
    # For ``src_has_array``: ``jsonb_array_elements_text(props -> '<prop>')``
    # For ``src_array_of_objects``: ``jsonb_array_elements(props -> '<prop>')``
    #   then access ``elt.val ->> '<src_match_field>'``.
    if norm.src_match_field is None:
        # scalar array — unnest as text
        unnest_sql = (
            f"jsonb_array_elements_text("
            f"{_prop_lookup_array('src', norm.src_array_prop)}"
            f") AS elt(val)"
        )
        src_match_value_sql = "elt.val"
    else:
        unnest_sql = (
            f"jsonb_array_elements("
            f"{_prop_lookup_array('src', norm.src_array_prop)}"
            f") AS elt(val)"
        )
        # Each element is a JSONB object; pull the named field as text.
        src_match_value_sql = (
            f"(elt.val ->> '{norm.src_match_field}')"
        )

    # ── dst-side value expression ─────────────────────────────────────
    # Chain of ``dst.properties ->> '<name>'`` joined by ``||``;
    # string literals become bound parameters.
    dst_pieces: list[str] = []
    for kind, value in norm.dst_expr.pieces:
        if kind == "prop":
            dst_pieces.append(_prop_lookup_text("dst", value))
        elif kind == "lit":
            params.append(value)
            dst_pieces.append(f"${len(params)}::text")
        else:  # pragma: no cover — defensive
            raise CompileError(f"unknown dst piece {kind!r}")
    dst_value_sql = " || ".join(dst_pieces) if len(dst_pieces) > 1 else dst_pieces[0]

    # ── match operator ────────────────────────────────────────────────
    if norm.match_op == "eq":
        match_sql = f"{src_match_value_sql} = {dst_value_sql}"
    elif norm.match_op == "eq_ci":
        match_sql = (
            f"lower({src_match_value_sql}) = lower({dst_value_sql})"
        )
    else:  # eq_norm — canonical fold on BOTH ends (see _normalize)
        match_sql = (
            f"{canon_text_sql(src_match_value_sql)} = "
            f"{canon_text_sql(dst_value_sql)}"
        )

    # ── endpoint-property comparators (require_*/_same) ───────────────
    extras: list[str] = []
    for prop in norm.require_different:
        extras.append(
            f"{_prop_lookup_text('src', prop)} "
            f"<> {_prop_lookup_text('dst', prop)}"
        )
    for prop in norm.require_same:
        extras.append(
            f"{_prop_lookup_text('src', prop)} "
            f"= {_prop_lookup_text('dst', prop)}"
        )
    # ``require_dst_property`` — fine-grained destination filter on
    # property values.  One ``ANY($N::text[])`` predicate per property
    # in the dict, ANDed together.  Values flow through the shared
    # ``params`` array so they're parameterised, not interpolated;
    # the property name is ``_check_ident``-validated above so it's
    # safe to embed in the JSONB accessor.
    for prop, allowed in norm.require_dst_property:
        params.append(list(allowed))
        extras.append(
            f"{_prop_lookup_text('dst', prop)} = ANY(${len(params)}::text[])"
        )
    extras_sql = (" AND " + " AND ".join(extras)) if extras else ""

    # ── arm body (SELECT … FROM …) ───────────────────────────────────
    # The caller wraps every arm into a single ``INSERT INTO
    # "<tenant>"."<EDGE_TYPE>" (start_id, end_id, properties) SELECT *
    # FROM ( <arms joined by UNION ALL> ) candidates ON CONFLICT … ``.
    # ``properties`` is agtype; we write the empty object.  ON CONFLICT
    # uses the ``(start_id, end_id)`` UNIQUE index installed at
    # bootstrap so re-running a resolve is a true no-op on existing
    # rows.
    return (
        f'SELECT src.id AS start_id, dst.id AS end_id, '
        f"'{{}}'::agtype AS properties "
        f'FROM "{tenant}"."{src_label}" src '
        f'CROSS JOIN "{tenant}"."{dst_label}" dst '
        f', {unnest_sql} '
        f'WHERE {match_sql}{extras_sql} '
        f'AND src.id <> dst.id'
    )


def _emit_one(
    *,
    tenant: str,
    norm: _NormalisedRule,
    src_label: str,
    dst_label: str,
) -> CompiledRule:
    """Single-pair emitter — wraps one arm in its own INSERT.

    Backs the per-pair ``compile_resolve_rule`` entry point (tests +
    introspection).  Non-test callers should use
    ``compile_resolve_rules_for_edge_type`` which collapses every pair
    of every rule into one INSERT.
    """
    params: list[object] = []
    arm = _emit_arm(
        tenant=tenant,
        norm=norm,
        src_label=src_label,
        dst_label=dst_label,
        params=params,
    )
    sql = (
        f'INSERT INTO "{tenant}"."{norm.edge_type}" '
        f'(start_id, end_id, properties) '
        f'{arm} '
        f'ON CONFLICT (start_id, end_id) DO NOTHING '
        f'RETURNING 1;'
    )
    return CompiledRule(
        sql=sql,
        params=params,
        src_label=src_label,
        dst_label=dst_label,
    )


def compile_resolve_rule(
    *,
    tenant: str,
    edge_type: str,
    rule: dict[str, object],
    catalogue: PropertyLabelCatalogue,
) -> list[CompiledRule]:
    """Compile one ``resolve_rule`` into a list of executable INSERTs.

    Returns one ``CompiledRule`` per (src_label, dst_label) candidate
    pair discovered via the catalogue.  Empty list when no candidate
    exists (no node carries the required properties yet) — caller
    should report zero edges written.

    NOTE: This is the per-pair API.  The HTTP resolve endpoint
    uses ``compile_resolve_rules_for_edge_type`` instead, which folds
    every pair of every rule for one edge_type into a single INSERT.

    Raises ``CompileError`` on any DSL violation or unsafe identifier.
    """
    norm = _normalise(edge_type, rule)
    src_labels, dst_labels = _infer_labels(norm, catalogue)
    if not src_labels or not dst_labels:
        return []
    out: list[CompiledRule] = []
    for sl in src_labels:
        for dl in dst_labels:
            out.append(_emit_one(
                tenant=tenant,
                norm=norm,
                src_label=sl,
                dst_label=dl,
            ))
    return out


@dataclass(frozen=True)
class CompiledEdgeType:
    """One INSERT collapsing every rule × every label pair for one
    ``edge_type``.

    * ``sql`` — parameterised statement, or ``None`` when no rule for
      this edge_type matched any catalogue entry (caller skips the DB
      round-trip and surfaces the no-vertex-label warning).
    * ``params`` — tuple of bind values, in $1, $2, … order.
    * ``arm_count`` — number of UNION ALL arms collapsed into the
      single SELECT.  Equals the sum over rules of (|src_labels| ×
      |dst_labels|) for that rule.
    * ``no_label_rule_count`` — how many of the input rules had no
      catalogue match at all.  When this equals ``len(rules)`` the
      endpoint surfaces the "no vertex label currently carries the
      properties" warning.
    * ``rule_count`` — total number of input rules (so the endpoint
      can compare against ``no_label_rule_count`` without re-counting).
    """

    edge_type: str
    sql: str | None
    params: tuple[object, ...]
    arm_count: int
    no_label_rule_count: int
    rule_count: int


def compile_resolve_rules_for_edge_type(
    *,
    tenant: str,
    edge_type: str,
    rules: list[dict[str, object]],
    catalogue: PropertyLabelCatalogue,
) -> CompiledEdgeType:
    """Collapse every ``resolve_rule`` for one ``edge_type`` into a
    single parameterised INSERT.

    Each rule contributes zero-or-more UNION ALL arms (one per
    (src_label, dst_label) catalogue candidate).  Rules whose required
    properties are not carried by any vertex label yet contribute zero
    arms but DO increment ``no_label_rule_count`` so the endpoint can
    surface the warning.

    When zero arms result across all rules, ``sql`` is ``None`` —
    caller should NOT execute anything against the DB.

    Raises ``CompileError`` on any DSL violation or unsafe identifier.
    """
    _check_ident(tenant, "tenant")
    _check_ident(edge_type, "edge_type")

    params: list[object] = []
    arms: list[str] = []
    no_label_rule_count = 0

    for rule in rules:
        norm = _normalise(edge_type, rule)
        src_labels, dst_labels = _infer_labels(norm, catalogue)
        if not src_labels or not dst_labels:
            no_label_rule_count += 1
            continue
        for sl in src_labels:
            for dl in dst_labels:
                arms.append(_emit_arm(
                    tenant=tenant,
                    norm=norm,
                    src_label=sl,
                    dst_label=dl,
                    params=params,
                ))

    if not arms:
        return CompiledEdgeType(
            edge_type=edge_type,
            sql=None,
            params=(),
            arm_count=0,
            no_label_rule_count=no_label_rule_count,
            rule_count=len(rules),
        )

    union = " UNION ALL ".join(arms)
    sql = (
        f'INSERT INTO "{tenant}"."{edge_type}" '
        f'(start_id, end_id, properties) '
        f'SELECT start_id, end_id, properties '
        f'FROM ( {union} ) candidates '
        f'ON CONFLICT (start_id, end_id) DO NOTHING '
        f'RETURNING 1;'
    )
    return CompiledEdgeType(
        edge_type=edge_type,
        sql=sql,
        params=tuple(params),
        arm_count=len(arms),
        no_label_rule_count=no_label_rule_count,
        rule_count=len(rules),
    )

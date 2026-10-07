"""Auto-derive functional indexes from tenant-registered ``resolve_rule``s.

The closed DSL on ``edge_labels[*].resolve_rule`` already tells graphos
exactly which JSONB properties a rule will touch in its compiled SQL.
That same information is enough to derive the supporting functional
indexes — graphos creates them on the tenant's vertex tables itself.
Consumers never write SQL DDL for resolver indexes; declaring the rule
is the only input.

Domain-agnostic by construction: this module sees property-name strings
and vertex-label names only — never the meaning of either.  Every
identifier compiled into DDL is guarded by the same strict regex the
compiler uses, so re-applying these indexes is injection-safe.

Idempotent: every ``CREATE INDEX`` is emitted with ``IF NOT EXISTS``,
named after a deterministic ``idx_<label>_<property>_<kind>`` slug
truncated to fit Postgres's 63-char identifier limit.  Re-registration
is therefore a no-op against an already-indexed schema.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

import asyncpg

from graphos.bootstrap import TenantSpec
from graphos.resolver._normalize import canon_text_sql
from graphos.resolver.catalogue import catalogue_for_properties

log = logging.getLogger(__name__)

# Same identifier shape the compiler accepts.  Anything that doesn't
# match cannot be safely interpolated into a CREATE INDEX statement.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ─── No-op short-circuit cache ──────────────────────────────────────────
# Process-scoped memo of the last *successful* ``ensure_resolve_rule_indexes``
# run per tenant.  Re-registration with an identical ``resolve_rules`` +
# ``meta`` spec is the common case (a consumer adapter may call
# ``POST /v1/tenants`` on every cache eviction), and the DDL loop
# is a multi-second no-op when nothing changed — 5–18 s on a large spec.
#
# We hash on ``(resolve_rules, meta)`` only — NOT on the catalogue snapshot.
# Rationale: the catalogue grows monotonically as writers load new
# labels, so a "different catalogue → re-run" rule would invalidate the
# cache constantly during a bulk load.  In steady state (read-only traffic
# against a stable schema) the spec hash is the right invalidation key.
#
# Consumers that need to force a re-run after seeding new labels can bump
# any ``meta`` value (e.g. ``meta={"schema_version": "<bump>"}``); that
# changes the hash and the next register re-derives indexes.
#
# Lost on graphos-api restart — the very next register pays the full DDL
# sweep, then steady state is free again.
_LAST_RUN: dict[str, tuple[str, list[str]]] = {}


def _spec_fingerprint(spec: TenantSpec) -> str:
    """Stable hash of the spec inputs that drive index derivation.

    Includes ``resolve_rules`` (which properties + which kind of index)
    and ``meta`` (consumer-controlled bump knob).  Excludes catalogue
    snapshot — see ``_LAST_RUN`` docstring.
    """
    payload = {
        "resolve_rules": [
            (edge_type, dict(rule))
            for edge_type, rule in spec.resolve_rules
        ],
        "meta": dict(spec.meta),
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()

# Postgres's NAMEDATALEN-1 cap on identifiers.  Index names longer than
# this get silently truncated server-side, which makes ``IF NOT EXISTS``
# unreliable (a re-run with a different rule could try to create the
# same truncated name).  We truncate ourselves, deterministically, with
# a short hash suffix to keep the name unique within its (label, prop,
# kind) tuple.
_MAX_IDENT_LEN = 63


def _check_ident(value: str, what: str) -> str:
    """Re-validate any identifier we're about to interpolate into DDL.

    The compiler applies the same check; this module duplicates it as
    defence-in-depth — the indexer runs on a different code path than
    the compiler and must not assume upstream validation ran.
    """
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        raise IndexerError(
            f"{what} {value!r} is not a safe identifier "
            f"(must match {_IDENT.pattern})"
        )
    return value


class IndexerError(ValueError):
    """Raised when a resolve_rule references an unsafe identifier.

    Bubbles up as HTTP 422 from the registration endpoint — the rule
    will not be persisted and no DDL will be issued.
    """


def _index_name(label: str, prop: str, kind: str) -> str:
    """Compose a deterministic index name for one ``(label, prop, kind)``.

    Kind is one of ``gin`` (array-container GIN), ``btree`` (scalar
    btree functional index on ``properties ->> '<prop>'``), or ``lower``
    (case-insensitive btree on ``lower(properties ->> '<prop>')``).

    Truncates to fit Postgres's identifier limit by appending a short
    deterministic hash so distinct ``(label, prop, kind)`` tuples
    never collide after truncation.
    """
    base = f"idx_{label}_{prop}_{kind}"
    if len(base) <= _MAX_IDENT_LEN:
        return base
    # Reserve 9 chars for "_" + 8-char hex digest.
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    head = base[: _MAX_IDENT_LEN - 9]
    return f"{head}_{digest}"


@dataclass(frozen=True)
class _IndexSpec:
    """One concrete index we want to ensure exists.

    ``kind`` is the structural family; ``ddl`` is the fully-formed
    ``CREATE INDEX IF NOT EXISTS ...`` statement ready to issue against
    the pool.  All identifiers inside ``ddl`` have already been
    validated against ``_IDENT``.
    """

    name: str
    label: str
    prop: str
    kind: str  # "gin" | "btree" | "lower"
    ddl: str


def _gin_index(tenant: str, label: str, prop: str) -> _IndexSpec:
    """GIN index on ``properties -> '<prop>'`` — for unnesting JSONB arrays."""
    name = _index_name(label, prop, "gin")
    ddl = (
        f'CREATE INDEX IF NOT EXISTS "{name}" '
        f'ON "{tenant}"."{label}" '
        f"USING gin ((((properties::text)::jsonb) -> '{prop}'));"
    )
    return _IndexSpec(name=name, label=label, prop=prop, kind="gin", ddl=ddl)


def _btree_index(tenant: str, label: str, prop: str) -> _IndexSpec:
    """Btree functional index on ``properties ->> '<prop>'`` — for
    scalar equality / inequality joins (``dst_value_from``, require_*)."""
    name = _index_name(label, prop, "btree")
    ddl = (
        f'CREATE INDEX IF NOT EXISTS "{name}" '
        f'ON "{tenant}"."{label}" '
        f"((((properties::text)::jsonb) ->> '{prop}'));"
    )
    return _IndexSpec(name=name, label=label, prop=prop, kind="btree", ddl=ddl)


def _lower_index(tenant: str, label: str, prop: str) -> _IndexSpec:
    """Btree on ``lower(properties ->> '<prop>')`` — case-insensitive eq_ci."""
    name = _index_name(label, prop, "lower")
    ddl = (
        f'CREATE INDEX IF NOT EXISTS "{name}" '
        f'ON "{tenant}"."{label}" '
        f"((lower(((properties::text)::jsonb) ->> '{prop}')));"
    )
    return _IndexSpec(name=name, label=label, prop=prop, kind="lower", ddl=ddl)


def _norm_index(tenant: str, label: str, prop: str) -> _IndexSpec:
    """Btree on ``canon(properties ->> '<prop>')`` — for ``eq_norm``.

    Uses the SAME canonical fold (``_normalize.canon_text_sql``) the
    compiler wraps its join in, so the planner can use this index for
    the ``eq_norm`` equality.  ``kind='norm'`` keeps the index name
    distinct from the plain btree / lower variants.
    """
    name = _index_name(label, prop, "norm")
    inner = f"((properties::text)::jsonb) ->> '{prop}'"
    ddl = (
        f'CREATE INDEX IF NOT EXISTS "{name}" '
        f'ON "{tenant}"."{label}" '
        f"(({canon_text_sql(inner)}));"
    )
    return _IndexSpec(name=name, label=label, prop=prop, kind="norm", ddl=ddl)


def _concat_index_name(
    label: str, pieces: list[tuple[str, str]], *, kind: str = "concat",
) -> str:
    """Stable name for a composite functional index over a multi-piece
    ``dst_value_from`` expression.  Hash the structural shape so two
    rules that compile to the same expression share one index.

    ``kind`` (``concat`` / ``concat_ci`` / ``concat_norm``) is folded
    into the hash so the plain, lower-folded, and canonical-folded
    variants over the same pieces get distinct names — otherwise their
    differing DDLs would collide in the name-keyed deduper and only one
    would be created.
    """
    sig = "_".join(f"{k}-{v}" for k, v in pieces)
    digest = hashlib.sha1(
        f"{label}|{kind}|{sig}".encode()
    ).hexdigest()[:10]
    base = f"idx_{label}_{kind}_{digest}"
    return base[:_MAX_IDENT_LEN]


def _concat_index(
    tenant: str,
    label: str,
    pieces: list[tuple[str, str]],
    *, fold: str = "none",
) -> _IndexSpec:
    """Composite btree functional index matching a multi-piece
    ``dst_value_from`` expression: ``prop || 'lit' || prop || ...``.

    ``pieces`` is the ordered tuple list the compiler also walks
    (``("prop", "<name>")`` for a property reference,
    ``("lit", "<string>")`` for a string literal).  The DDL inlines
    string literals — Postgres functional indexes require IMMUTABLE
    expressions with no parameters.  Identifier and literal escaping
    is conservative; the registration validator + the compiler's own
    ``_check_ident`` cover the safety net.

    The single-piece case (one property, no literals) is handled by
    the plain ``_btree_index`` and is NOT emitted here — that would
    duplicate the per-prop btree.
    """
    fragments: list[str] = []
    for kind, value in pieces:
        if kind == "prop":
            _check_ident(value, "dst_value_from prop")
            fragments.append(f"(((properties::text)::jsonb) ->> '{value}')")
        elif kind == "lit":
            # Conservative single-quote escape — the DSL is meant for
            # short separators ('.', '/', etc.); SQL injection via a
            # literal is prevented by doubling any embedded quote.
            esc = value.replace("'", "''")
            fragments.append(f"'{esc}'")
        else:  # pragma: no cover — defensive
            raise IndexerError(f"unknown dst piece kind {kind!r}")
    expr = " || ".join(fragments)
    if fold == "lower":
        expr = f"lower({expr})"
        kind = "concat_ci"
    elif fold == "canon":
        expr = canon_text_sql(expr)
        kind = "concat_norm"
    else:
        kind = "concat"
    name = _concat_index_name(label, pieces, kind=kind)
    ddl = (
        f'CREATE INDEX IF NOT EXISTS "{name}" '
        f'ON "{tenant}"."{label}" '
        f"(({expr}));"
    )
    # ``prop`` on the spec gets the joined identifier list — purely
    # informational; the deduper keys on ``name``.
    prop_marker = "+".join(v for k, v in pieces if k == "prop")
    return _IndexSpec(
        name=name, label=label, prop=prop_marker,
        kind=kind, ddl=ddl,
    )


def _dst_value_pieces(expr: str) -> list[tuple[str, str]] | None:
    """Parse ``dst_value_from`` into the same ``[(kind, value), ...]``
    sequence the compiler emits.  Mirrors the compiler's surface:
    bare identifier OR ``concat(arg, arg, ...)`` where each arg is
    either an identifier or a single-quoted string literal.

    Returns ``None`` when the expression is empty or unparsable here —
    the registration-side validator is the authoritative shape check;
    this helper is best-effort for the indexer's optional composite
    optimisation.  When ``None``, the indexer falls back to the
    per-prop btree path.
    """
    s = expr.strip()
    if not s:
        return None
    # Bare identifier — handled by per-prop btree, no composite needed.
    if _IDENT.fullmatch(s):
        return [("prop", s)]
    if not s.startswith("concat(") or not s.endswith(")"):
        return None
    inner = s[len("concat("):-1]
    pieces: list[tuple[str, str]] = []
    # Tiny recursive-descent: walk the arg list, respecting single-
    # quoted strings (with doubled-quote escape) and identifiers.
    i, n = 0, len(inner)
    while i < n:
        # Skip whitespace + commas.
        while i < n and inner[i] in ", \t\n":
            i += 1
        if i >= n:
            break
        if inner[i] == "'":
            # String literal — single quotes, doubled-quote escape.
            j = i + 1
            buf: list[str] = []
            while j < n:
                if inner[j] == "'":
                    if j + 1 < n and inner[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(inner[j])
                j += 1
            if j >= n:
                return None  # unterminated literal — let validator handle
            pieces.append(("lit", "".join(buf)))
            i = j + 1
        else:
            # Identifier.
            j = i
            while j < n and (inner[j].isalnum() or inner[j] == "_"):
                j += 1
            ident = inner[i:j]
            if not ident or not _IDENT.fullmatch(ident):
                return None
            pieces.append(("prop", ident))
            i = j
    return pieces or None


# ── DSL → index-kind derivation ─────────────────────────────────────────


# ``dst_value_from`` is a tiny expression: a bare identifier OR a
# ``concat(arg, arg, ...)`` call where each arg is either an identifier
# or a single-quoted string literal.  This regex pulls out every bare
# identifier in the expression — those are the property names we need
# btree indexes on.  String literals are skipped (they're parameters,
# not column references).
_DST_PROP_TOKEN = re.compile(
    r"(?:'(?:[^'\\]|\\.)*')"   # string literal — skipped
    r"|"
    r"([A-Za-z_][A-Za-z0-9_]*)"  # captured identifier
)
# Reserved tokens that look like identifiers but are part of the DSL
# grammar, not property names.
_DST_RESERVED: frozenset[str] = frozenset({"concat"})


def _dst_value_property_names(expr: str) -> list[str]:
    """Pull every bare property identifier out of a ``dst_value_from``
    expression.  String literals are ignored (they bind as parameters).

    Returns identifiers in source order, deduped.  Each is re-validated
    against ``_IDENT``.
    """
    seen: dict[str, None] = {}
    for m in _DST_PROP_TOKEN.finditer(expr):
        ident = m.group(1)
        if ident is None or ident in _DST_RESERVED:
            continue
        _check_ident(ident, "dst_value_from identifier")
        seen.setdefault(ident, None)
    return list(seen.keys())


def _array_property_for_rule(rule: dict[str, object]) -> str | None:
    """Return the src-side array property name (the one that needs GIN),
    or ``None`` if the rule has none (defensive — registration validation
    already requires exactly one of these two keys to be set).
    """
    for key in ("src_has_array", "src_array_of_objects"):
        val = rule.get(key)
        if val is not None:
            return _check_ident(str(val), key)
    return None


def _scalar_properties_for_rule(rule: dict[str, object]) -> list[str]:
    """Every property name that ends up in a scalar comparison: the
    ``dst_value_from`` props + every ``require_different`` /
    ``require_same`` prop + every ``require_dst_property`` key.  These
    all want a btree functional index on the labels that carry them
    so the resolver's emitted ``properties ->> '<prop>'`` predicates
    plan as index-scans, not seq-scans.

    ``src_array_of_objects`` rules also need ``src_match_field`` — but
    that field is read out of each unnested JSONB *element*, not the
    parent row's properties column, so it does NOT get an index here.
    """
    out: dict[str, None] = {}
    expr = rule.get("dst_value_from")
    if isinstance(expr, str) and expr.strip():
        for ident in _dst_value_property_names(expr):
            out.setdefault(ident, None)
    for key in ("require_different", "require_same"):
        seq = rule.get(key) or []
        if not isinstance(seq, Iterable) or isinstance(seq, (str, bytes)):
            continue
        for name in seq:
            if not isinstance(name, str):
                continue
            _check_ident(name, key)
            out.setdefault(name, None)
    # ``require_dst_property`` adds ``dst.properties ->> '<key>' =
    # ANY($N::text[])`` predicates to every emitted resolver SQL arm.
    # Each property key wants the same btree functional index shape
    # as ``dst_value_from`` / ``require_*`` properties get.  Without
    # this loop, a ``kind`` filter declared via ``require_dst_property``
    # plans as a heap scan on the destination label on every
    # resolve.
    dst_props = rule.get("require_dst_property")
    if isinstance(dst_props, dict):
        for name in dst_props:
            if not isinstance(name, str):
                continue
            _check_ident(name, "require_dst_property")
            out.setdefault(name, None)
    return list(out.keys())


def _match_fold(rule: dict[str, object]) -> str:
    """Map ``match_op`` to the dst-side fold the compiler emits.

    Returns ``"lower"`` for ``eq_ci``, ``"canon"`` for ``eq_norm``, or
    ``"none"`` for plain ``eq``.  Only the ``dst_value_from`` properties
    need the folded functional index — those are the values the compiler
    wraps.  ``require_*`` properties always use plain ``->>`` equality.
    """
    op = str(rule.get("match_op", "eq"))
    if op == "eq_ci":
        return "lower"
    if op == "eq_norm":
        return "canon"
    return "none"


def _wants_case_insensitive(rule: dict[str, object]) -> bool:
    """Back-compat shim — true when the rule needs any folded variant."""
    return _match_fold(rule) != "none"


# ── Public entry point ──────────────────────────────────────────────────


async def ensure_resolve_rule_indexes(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    spec: TenantSpec,
) -> list[str]:
    """Ensure functional indexes exist for every property a rule references.

    For each ``(edge_type, resolve_rule)`` pair on the spec:

    * ``src_has_array`` / ``src_array_of_objects`` → GIN index on
      ``properties -> '<prop>'`` for every label that carries ``<prop>``.
    * ``dst_value_from`` property names → btree functional index on
      ``properties ->> '<prop>'`` for every label that carries them.
      When ``match_op == 'eq_ci'``, also adds the ``lower(...)`` variant.
    * ``require_different`` / ``require_same`` → btree functional index
      on ``properties ->> '<prop>'``.

    Candidate labels per property come from the per-tenant property →
    labels catalogue (the same one the compiler uses).  A property that
    no node currently carries logs a warning and gets no index — when a
    later node arrives with that property, a subsequent re-registration
    will pick it up.

    Returns the deduped list of index names that were ensured (created
    or already-present).  Idempotent — re-registration produces the
    same DDL and Postgres no-ops thanks to ``IF NOT EXISTS``.
    """
    _check_ident(tenant, "tenant")

    if not spec.resolve_rules:
        return []

    # ── Fast-path: no-op re-registration ───────────────────────────────
    # If the (resolve_rules, meta) fingerprint matches the last
    # successful run for this tenant, every CREATE INDEX IF NOT EXISTS
    # below would be a server-side no-op anyway — but the catalogue
    # refresh + DDL plan + identifier guarding still costs 5-18 s
    # on a 147-index spec.  Return the cached ensured list
    # and skip the work.  See ``_LAST_RUN`` docstring for cache scope
    # and invalidation knob.
    fingerprint = _spec_fingerprint(spec)
    cached = _LAST_RUN.get(tenant)
    if cached is not None and cached[0] == fingerprint:
        log.info(
            "resolver.indexer: skipped (spec unchanged) for tenant %r "
            "n_indexes=%d",
            tenant, len(cached[1]),
            extra={"tenant": tenant, "n_indexes": len(cached[1])},
        )
        return list(cached[1])

    # The labels carrying each property the rules index, read now.
    wanted: set[str] = set()
    for _, rule in spec.resolve_rules:
        arr = _array_property_for_rule(rule)
        wanted.update(([arr] if arr else []) + _scalar_properties_for_rule(rule))
    catalogue = await catalogue_for_properties(pool, tenant, wanted)

    # Build the set of indexes to ensure.  Dedupe by name across rules
    # so two rules that need the same ``(label, prop, kind)`` only emit
    # one DDL.
    specs_by_name: dict[str, _IndexSpec] = {}
    missing_property_warnings: dict[str, set[str]] = {}

    def _register(specs: list[_IndexSpec]) -> None:
        for s in specs:
            specs_by_name.setdefault(s.name, s)

    def _labels_or_warn(prop: str, edge_type: str) -> list[str]:
        labels = catalogue.labels_for(prop)
        if not labels:
            missing_property_warnings.setdefault(prop, set()).add(edge_type)
            return []
        # Defence-in-depth: drop anything that wouldn't survive the
        # identifier guard.  ``catalogue.refresh`` already filters this,
        # but the cache may have been seeded by test code.
        return sorted(lab for lab in labels if _IDENT.fullmatch(lab))

    for edge_type, rule in spec.resolve_rules:
        _check_ident(edge_type, "edge_type")

        # ── GIN on the src-side array property ──────────────────────
        arr_prop = _array_property_for_rule(rule)
        if arr_prop is not None:
            for label in _labels_or_warn(arr_prop, edge_type):
                _register([_gin_index(tenant, label, arr_prop)])

        # ── Btree on every scalar comparison property ───────────────
        scalar_props = _scalar_properties_for_rule(rule)
        for prop in scalar_props:
            for label in _labels_or_warn(prop, edge_type):
                _register([_btree_index(tenant, label, prop)])

        # ── folded variant for case-insensitive / canonical matches ─
        fold = _match_fold(rule)
        if fold != "none":
            expr = rule.get("dst_value_from")
            if isinstance(expr, str) and expr.strip():
                builder = _norm_index if fold == "canon" else _lower_index
                for prop in _dst_value_property_names(expr):
                    for label in _labels_or_warn(prop, edge_type):
                        _register([builder(tenant, label, prop)])

        # ── Composite index for multi-piece dst_value_from ──────────
        # When ``dst_value_from`` is ``concat(prop, 'lit', prop, ...)``
        # the per-prop btrees DON'T cover the join condition the
        # compiler emits (``prop1 || 'lit' || prop2``).  Postgres
        # needs a functional index on the FULL concatenated
        # expression to use it.  Single-piece bare-identifier
        # expressions are already covered by the per-prop btree
        # above and don't need this.
        expr = rule.get("dst_value_from")
        if isinstance(expr, str) and expr.strip():
            pieces = _dst_value_pieces(expr)
            if pieces is not None and (
                len(pieces) > 1 or pieces[0][0] == "lit"
            ):
                # Composite covers ALL pieces — only label that
                # carries every referenced property is a useful host.
                concat_props = [v for k, v in pieces if k == "prop"]
                if concat_props:
                    # Labels carrying every concat_props member.
                    label_sets = [
                        set(catalogue.labels_for(p)) for p in concat_props
                    ]
                    common = (
                        set.intersection(*label_sets)
                        if label_sets else set()
                    )
                    for label in sorted(
                        lab for lab in common if _IDENT.fullmatch(lab)
                    ):
                        _register([_concat_index(
                            tenant, label, pieces,
                            fold=_match_fold(rule),
                        )])

    if missing_property_warnings:
        log.info(
            "resolver.indexer: properties not yet present in catalogue; "
            "no index created (will be picked up on re-registration once "
            "a node carries the property)",
            extra={
                "tenant": tenant,
                "missing": {
                    prop: sorted(edges)
                    for prop, edges in missing_property_warnings.items()
                },
            },
        )

    if not specs_by_name:
        return []

    # Issue the DDL.  ``IF NOT EXISTS`` makes each statement a no-op
    # when the index already exists; we still log per-create attempts at
    # DEBUG so an operator can trace which indexes a registration
    # touched.
    ensured: list[str] = []
    async with pool.acquire() as conn:
        for s in specs_by_name.values():
            try:
                await conn.execute(s.ddl)
                ensured.append(s.name)
            except asyncpg.exceptions.PostgresError as exc:
                # An error on a single index does not abort the whole
                # registration — log and continue so the rest of the
                # indexes still land.  The most common cause in
                # practice is the label table not existing yet because
                # the tenant has zero nodes; that's harmless and
                # self-healing once data lands and a re-register runs.
                log.warning(
                    "resolver.indexer: index create failed (skipped)",
                    extra={
                        "tenant": tenant,
                        "index": s.name,
                        "label": s.label,
                        "prop": s.prop,
                        "kind": s.kind,
                        "error": str(exc),
                    },
                )

    log.info(
        "resolver.indexer: ensured %d functional indexes for tenant %r",
        len(ensured), tenant,
        extra={"tenant": tenant, "n_indexes": len(ensured)},
    )
    # Record successful run for the no-op short-circuit on the next
    # identical re-registration.  Only cache on FULL success — if any
    # DDL was skipped via the per-statement except above, the next
    # call must retry the failed indexes (don't poison the cache).
    if len(ensured) == len(specs_by_name):
        _LAST_RUN[tenant] = (fingerprint, list(ensured))
    return ensured

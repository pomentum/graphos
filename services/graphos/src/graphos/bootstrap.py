"""Tenant bootstrap routine.

graphos is **tenant-agnostic**. There are no hardcoded tenant specs; consumers
register their own context at runtime via ``POST /v1/tenants``. The
registration payload carries the full schema (vertex labels, edge labels,
indexes, generated columns), which this module turns into idempotent SQL
against the AGE-extended postgres.

Two reasons graphos doesn't own tenant schemas:

1. **No release coupling.** When a consumer adds a new node/edge type to its
   own contract, it just re-registers; graphos doesn't need a deploy.
2. **Reuse-as-a-service.** Any consumer with a token can claim a tenant name
   and operate inside its own AGE graph.

State:
    The in-process registry (``_REGISTERED``) is a runtime cache. It is
    populated by ``register_tenant()`` AND restored on graphos-api startup
    from ``graphos_meta.tenants`` (``load_registered_tenants()``). So
    consumers don't have to re-register after a graphos-api restart, but
    re-registering is still cheap (idempotent) and safe.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field

import asyncpg

from graphos.db import DDL_TIMEOUT_S, allow_long_statements

log = logging.getLogger(__name__)

# A persisted resolve rule is stored as a ``[name, rule]`` pair.
_RULE_PAIR_LEN = 2


# ── Tenant spec ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TenantSpec:
    """Declarative schema for one tenant's AGE graph.

    Supplied by the consumer at register time; turned into idempotent SQL
    by ``bootstrap_tenant()``.

    Fields:
        name:               AGE graph name (also the URL path segment).
        vertex_labels:      Each becomes ``create_vlabel(name, label)``.
        edge_labels:        Each becomes ``create_elabel(name, label)``.
        indexes:            Each is a full ``CREATE INDEX IF NOT EXISTS ...``
                            statement. Lets consumers pick any pg index form
                            (functional, partial, GIN, …) targeting the AGE
                            per-label storage tables.
        generated_columns:  Each is a full ``ALTER TABLE ... ADD COLUMN ...
                            GENERATED ALWAYS AS (...) STORED;`` statement.
                            Guarded by ``duplicate_column`` exception swallow.
        meta:               Free-form consumer-supplied metadata; surfaced on
                            ``GET /v1/tenants``.
    """

    name: str
    vertex_labels: tuple[str, ...] = ()
    edge_labels: tuple[str, ...] = ()
    indexes: tuple[str, ...] = ()
    generated_columns: tuple[str, ...] = ()
    # Per-edge-label resolve rules, mapping edge_type → resolve_rule
    # JSONB body.  Drives the schema-driven property-match edge
    # resolver (``POST /v1/{tenant}/graph/edges:resolve``).  Empty when
    # the consumer hasn't opted any edge type into automatic resolution.
    # See ``graphos.resolver`` and the closed-DSL contract in
    # ``graphos.contracts.tenants._validate_resolve_rule``.
    resolve_rules: tuple[tuple[str, dict[str, object]], ...] = ()
    # Postgres-inheritance edges between vertex labels, expressed as
    # (child, parent) pairs.  AGE 1.6 doesn't support multi-label
    # syntax (``MERGE (n:L1:L2 ...)`` is a parse error), but
    # ``ALTER TABLE child INHERIT parent`` on the per-label storage
    # tables gives equivalent semantics: writes to ``:Child`` are
    # visible to ``MATCH (n:Parent)`` via postgres's table-inheritance
    # query rewrite — verified end-to-end against AGE 1.6.  Both sides
    # of each pair must appear in ``vertex_labels``.
    label_hierarchy: tuple[tuple[str, str], ...] = ()
    meta: dict[str, str] = field(default_factory=dict)


# ── In-process registry ─────────────────────────────────────────────────────

# Holds the currently-registered specs for this graphos-api process.
# Populated by ``register_tenant()``; read by the GET endpoint.
_REGISTERED: dict[str, TenantSpec] = {}


def registered_tenants() -> dict[str, TenantSpec]:
    return dict(_REGISTERED)


def remember(spec: TenantSpec) -> None:
    _REGISTERED[spec.name] = spec


# ── Idempotent execution helper ─────────────────────────────────────────────

# Postgres / AGE sqlstate codes for "already exists" — what bootstrap_tenant
# must swallow to be idempotent. asyncpg surfaces these as `exc.sqlstate`
# (5-char code), not as the long name; we match on codes.
_SWALLOW_DUPLICATE_OBJECT = {
    "42710",   # duplicate_object — create_vlabel/create_elabel
    "42P06",   # duplicate_schema — alt shape for graph-already-exists
    "42P07",   # duplicate_table — defensive: AGE storage table re-create
    "3F000",   # invalid_schema_name — AGE create_graph "graph X already exists"
}
_SWALLOW_DUPLICATE_COLUMN = {
    "42701",   # duplicate_column — ALTER TABLE ... ADD COLUMN re-run
}


async def _safe(
    conn: asyncpg.Connection,
    sql: str,
    *,
    swallow: set[str],
) -> None:
    """Execute SQL; swallow named sqlstate exceptions (idempotency)."""
    try:
        await conn.execute(sql)
    except asyncpg.exceptions.PostgresError as exc:
        if exc.sqlstate not in swallow:
            raise


# ── Bootstrap routine ───────────────────────────────────────────────────────


async def _inherit_if_missing(
    conn: asyncpg.Connection,
    graph: str,
    child: str,
    parent: str,
) -> bool:
    """Wire postgres table inheritance ``child INHERITS parent``.

    Idempotent: queries ``pg_inherits`` first; only fires the
    ``ALTER TABLE`` when the inheritance link doesn't already exist.
    Returns ``True`` if a new inheritance edge was created, ``False``
    if it was already in place.

    Re-running ``ALTER TABLE … INHERIT …`` on an existing edge fails
    with ``relation would be inherited from more than once`` — there's
    no ``IF NOT EXISTS`` form for INHERIT.  The catalog probe is the
    canonical way to make this idempotent.

    AGE label-name handling: ``create_vlabel`` quotes the label
    literally, so a label ``"Article"`` becomes a postgres relation
    named ``"Article"`` in the graph's schema.  We quote the same way.
    """
    exists = await conn.fetchval(
        """
        SELECT 1
          FROM pg_inherits i
          JOIN pg_class    c ON i.inhrelid  = c.oid
          JOIN pg_namespace n ON c.relnamespace = n.oid
          JOIN pg_class    p ON i.inhparent = p.oid
         WHERE n.nspname = $1
           AND c.relname = $2
           AND p.relname = $3;
        """,
        graph, child, parent,
    )
    if exists:
        return False
    await conn.execute(
        f'ALTER TABLE "{graph}"."{child}" INHERIT "{graph}"."{parent}";'
    )
    return True


def _validate_label_hierarchy(
    pairs: tuple[tuple[str, str], ...],
    vertex_labels: tuple[str, ...],
) -> None:
    """Refuse hierarchy entries that reference unregistered labels or
    that form a self-loop.  Cycle detection is left to postgres —
    ``ALTER TABLE … INHERIT …`` errors with ``inheritance loop``
    sqlstate ``42P07`` when a cycle would be created.
    """
    known = set(vertex_labels)
    for child, parent in pairs:
        if child == parent:
            raise ValueError(
                f"label_hierarchy entry {child!r}->{parent!r}: self-loop"
            )
        if child not in known:
            raise ValueError(
                f"label_hierarchy child {child!r} not in vertex_labels"
            )
        if parent not in known:
            raise ValueError(
                f"label_hierarchy parent {parent!r} not in vertex_labels"
            )


async def bootstrap_tenant(pool: asyncpg.Pool, spec: TenantSpec) -> None:
    """Bring a tenant's AGE graph up to spec. Idempotent.

    Concurrency: the body issues a long sequence of DDL statements
    (``create_vlabel``, ``CREATE INDEX IF NOT EXISTS``, …) under a
    *single* connection.  ``IF NOT EXISTS`` is **not race-safe** in
    PostgreSQL — two concurrent transactions both pass the existence
    check, both try to allocate the same ``pg_class.relname``, and
    the loser raises ``UniqueViolationError`` on
    ``pg_class_relname_nsp_index``.  Symptom on the caller side: the
    first writer bootstraps the tenant; siblings firing in
    parallel race and one (or more) gets a 500 from this endpoint,
    which propagates up the caller's pipeline as a hard failure.

    Fix: serialise bootstrap per-tenant via a session-level
    ``pg_advisory_lock`` keyed on ``spec.name``.  All concurrent
    callers for the same tenant queue at the lock; once they
    acquire it, the body's ``IF EXISTS``/``IF NOT EXISTS`` /
    ``_SWALLOW_DUPLICATE_OBJECT`` guards make every step a no-op
    on already-bootstrapped state.  Different tenants don't
    contend (different lock keys).  Released in a ``finally`` so
    a mid-bootstrap exception doesn't strand the lock; advisory
    locks are also auto-released when the session ends.
    """
    _validate_label_hierarchy(spec.label_hierarchy, spec.vertex_labels)
    log.info(
        "bootstrap tenant",
        extra={
            "tenant": spec.name,
            "vlabels": len(spec.vertex_labels),
            "elabels": len(spec.edge_labels),
            "indexes": len(spec.indexes),
            "generated_cols": len(spec.generated_columns),
            "inheritance_edges": len(spec.label_hierarchy),
        },
    )
    async with pool.acquire() as conn:
        await allow_long_statements(conn, DDL_TIMEOUT_S)
        # AGE is loaded by the per-connection hook (db.py) or preloaded by the
        # server; the search_path is re-applied here for safety.
        await conn.execute('SET search_path = ag_catalog, "$user", public;')

        # Per-tenant bootstrap serialisation.  Held across the whole
        # DDL sequence below (sections 1-6); released in the matching
        # ``finally`` block.  See the bootstrap_tenant() docstring above
        # for the race condition this guards against.
        await conn.execute(
            "SELECT pg_advisory_lock(hashtextextended($1, 0));",
            spec.name,
        )
        try:
            # 1. Graph itself
            await _safe(
                conn,
                f"SELECT create_graph('{spec.name}');",
                swallow=_SWALLOW_DUPLICATE_OBJECT,
            )

            # 2. Vertex labels + per-label GIN(properties) index
            # AGE's ``MATCH (n:Label {id: X})`` compiles to
            # ``WHERE properties @> '{"id":"X"}'::agtype`` -- without a
            # GIN index on the properties column, every MERGE / MATCH
            # falls back to a sequential scan of the label table.
            # Empirically: 6ms -> 0.4ms (~15x) at 4k rows; the gap
            # widens linearly with table size.  Idempotent via
            # ``CREATE INDEX IF NOT EXISTS``.
            for label in spec.vertex_labels:
                await _safe(
                    conn,
                    f"SELECT create_vlabel('{spec.name}', '{label}');",
                    swallow=_SWALLOW_DUPLICATE_OBJECT,
                )
                await conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "{label}_properties_gin" '
                    f'ON "{spec.name}"."{label}" USING gin (properties);'
                )
                # UNIQUE expression index on the external id ``properties->'"id"'``.
                # Required by the COPY-based bulk upsert path
                # (``bulk_upsert_nodes_via_copy``) — ``INSERT … ON CONFLICT
                # ((properties->'"id"'))`` resolves to this index for the
                # dedup/property-merge semantics that ``cypher MERGE`` provides
                # on the per-row write path.  Per-vertex-label so ``:Article`` and
                # ``:Note`` with the same external id don't collide.
                # Idempotent via IF NOT EXISTS.
                await conn.execute(
                    f'CREATE UNIQUE INDEX IF NOT EXISTS "{label}_id_unique" '
                    f'ON "{spec.name}"."{label}" ((properties->\'"id"\'));'
                )
                # Indexes for the properties graphos itself reads.  They are
                # not covered by GIN containment (``@>``), which cannot serve
                # scalar ``->>`` equality or ``LIKE``; without them the
                # planner seq-scans every label table.
                #
                # * ``community_id`` — written by Louvain, read by the
                #   community endpoints and community-scoped ``expand``.
                #   Measured 2.4 s -> 0.5 ms on a 117k-row label.
                # * trigram GIN on ``name`` — serves ``/graph/search``
                #   (``name ILIKE '%q%'``), which a btree cannot do for a
                #   leading ``%``.  Measured ~1.2 s -> ~1 ms on the same label.
                #
                # Any other hot property is the consumer's to declare via the
                # ``indexes`` field of the registration (see contracts/ddl.py).
                # ``pg_trgm`` ships with Postgres; creating it is idempotent.
                await conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "{label}_community_id" '
                    f'ON "{spec.name}"."{label}" '
                    f"( (((properties::text)::jsonb) ->> 'community_id') );"
                )
                await _safe(
                    conn,
                    "CREATE EXTENSION IF NOT EXISTS pg_trgm;",
                    swallow=_SWALLOW_DUPLICATE_OBJECT,
                )
                await conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "{label}_name_trgm" '
                    f'ON "{spec.name}"."{label}" USING gin '
                    f'( (((properties::text)::jsonb) ->> \'name\') '
                    f'gin_trgm_ops );'
                )

            # 3. Edge labels + (start_id, end_id) btree index
            # Edge MERGE checks for an existing relationship by
            # ``(start_id, end_id, label)`` -- without a btree on the
            # endpoint columns the existence check is a sequential scan
            # over the per-edge-type table.

            # Parent-side ``end_community_id`` column.  Postgres inheritance
            # requires the column to exist on BOTH parent and child for
            # queries that scan through the parent to see it.  /expand
            # queries ``FROM "{tenant}"."_ag_label_edge"`` (the parent)
            # so we add the column here once.  Children inherit the
            # column via the per-label ALTER below — though Postgres
            # would also propagate parent ADD COLUMN to existing
            # children, we keep both explicit so the partial btree (per
            # child) is unambiguously per-table.
            await _safe(
                conn,
                f'ALTER TABLE "{spec.name}"."_ag_label_edge" '
                f'ADD COLUMN IF NOT EXISTS end_community_id text;',
                swallow=_SWALLOW_DUPLICATE_COLUMN,
            )

            for label in spec.edge_labels:
                await _safe(
                    conn,
                    f"SELECT create_elabel('{spec.name}', '{label}');",
                    swallow=_SWALLOW_DUPLICATE_OBJECT,
                )
                # Promote the existing btree(start_id, end_id) to UNIQUE so the
                # COPY-based bulk-upsert path's ``INSERT … ON CONFLICT
                # (start_id, end_id)`` resolves to this index.  Our edge model
                # is single-edge-per-(src, dst) per edge type — matches the
                # cypher MERGE semantics on the per-row path.  Dropping the
                # non-unique btree first if it exists is safe; the new UNIQUE
                # index covers the same lookup pattern.  Idempotent via
                # IF NOT EXISTS on the UNIQUE side; the plain btree is kept
                # (Postgres tolerates the redundancy).
                await conn.execute(
                    f'CREATE UNIQUE INDEX IF NOT EXISTS '
                    f'"{label}_endpoints_unique" '
                    f'ON "{spec.name}"."{label}" (start_id, end_id);'
                )
                # Keep the non-unique copy too for back-compat with any code
                # that's already relying on it; postgres picks the cheaper
                # index for MATCH at query plan time.
                await conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "{label}_endpoints_btree" '
                    f'ON "{spec.name}"."{label}" USING btree (start_id, end_id);'
                )
                # Reverse-direction lookup — neighbourhood expansion walks
                # incoming edges via ``WHERE end_id IN (...)``.  The
                # ``(start_id, end_id)`` btree above can't serve that
                # (leftmost-prefix rule: filter on the second column alone
                # won't use the index), so the planner falls back to a
                # parallel seq scan across every edge-label table.  A
                # single-column ``(end_id)`` btree closes the gap.
                # Empirical impact on a 6 M-edge graph:
                # ``WHERE end_id IN (3 ids) AND relname IN (...)``
                # 285 ms → 10 ms (~29x).
                await conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "{label}_end_id" '
                    f'ON "{spec.name}"."{label}" USING btree (end_id);'
                )
                # Denormalised end-vertex community for Louvain-scoped BFS.
                # The /expand BFS supports a ``communities=...`` query
                # param that bounds the traversal to vertices in a given
                # Louvain partition.  Naively this requires a per-hop semi-
                # join against ``_ag_label_vertex`` (expensive per call)
                # or a per-hop array-parameter hash build (catalog churn
                # at high QPS).  Both shift cost onto the read path on a
                # CPU-constrained box.
                #
                # Denormalise the end-vertex's community_id onto each edge
                # row instead: the Louvain writeback (see
                # ``louvain._writeback_assignments``) refreshes this
                # column after each rerun, and /expand becomes a single-
                # column predicate ``end_community_id = ANY ($N::text[])``
                # that the per-label partial btree below serves with a
                # bitmap-and against the existing ``(start_id, end_id)``
                # index.  Reads drop from ~150 ms / hop (temp-table or
                # unnest scope) to ~25 ms / hop.
                #
                # Type: ``text`` matches what ``->>'community_id'`` returns
                # in the existing JSONB-extract codepath; new edges arrive
                # NULL and become populated on the next Louvain pass —
                # ``WHERE end_community_id = ANY(...)`` drops NULL rows
                # naturally so they're invisible to scoped queries until
                # the next refresh (acceptable read freshness;
                # unscoped intents see them immediately).
                await _safe(
                    conn,
                    f'ALTER TABLE "{spec.name}"."{label}" '
                    f'ADD COLUMN IF NOT EXISTS end_community_id text;',
                    swallow=_SWALLOW_DUPLICATE_COLUMN,
                )
                # Partial index — most edges are NULL until Louvain runs;
                # indexing only the populated subset keeps the index
                # small + fast on the hot path.
                await conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "{label}_end_comm_id" '
                    f'ON "{spec.name}"."{label}" USING btree '
                    f'(end_community_id) WHERE end_community_id IS NOT NULL;'
                )

            # 4. Label hierarchy (postgres-inheritance multi-label
            # simulation).  Must run AFTER both vertex labels exist;
            # ``_inherit_if_missing`` checks pg_inherits first so
            # re-bootstrap of an already-wired tenant is a no-op.
            for child, parent in spec.label_hierarchy:
                await _inherit_if_missing(conn, spec.name, child, parent)

            # 5. Indexes (use native IF NOT EXISTS — caller is responsible)
            for ddl in spec.indexes:
                await conn.execute(ddl)

            # 6. Generated columns (no native IF NOT EXISTS; swallow duplicate_column)
            for ddl in spec.generated_columns:
                await _safe(conn, ddl, swallow=_SWALLOW_DUPLICATE_COLUMN)


        finally:
            # Best-effort release — see bootstrap_tenant() docstring.
            try:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0));",
                    spec.name,
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    "bootstrap lock release failed (connection likely dead)",
                    extra={"tenant": spec.name},
                )

def _spec_to_jsonable(spec: TenantSpec) -> dict[str, object]:
    """Convert a TenantSpec to the JSONB-friendly shape stored in graphos_meta.

    ``asdict()`` turns the tuples into lists — that's already what JSON wants.
    """
    return asdict(spec)


def _spec_from_jsonable(payload: dict[str, object]) -> TenantSpec:
    # ``label_hierarchy`` round-trips as a list of [child, parent]
    # pairs because asdict() on a tuple-of-tuples produces nested
    # lists in JSON.  Tolerate both shapes (dict or list-of-pairs)
    # for forward compat with the wire body that uses a dict.
    raw = payload.get("label_hierarchy") or ()
    if isinstance(raw, dict):
        hierarchy_pairs = tuple((str(k), str(v)) for k, v in raw.items())
    else:
        hierarchy_pairs = tuple(
            (str(p[0]), str(p[1])) for p in raw
        )
    # ``resolve_rules`` round-trips as either a dict ``{edge_type:
    # rule_dict}`` (preferred wire shape) or a list of
    # ``[edge_type, rule_dict]`` pairs (asdict on tuple-of-tuples).
    rules_raw = payload.get("resolve_rules") or ()
    if isinstance(rules_raw, dict):
        rules_pairs = tuple(
            (str(k), dict(v)) for k, v in rules_raw.items()
            if isinstance(v, dict)
        )
    else:
        rules_pairs = tuple(
            (str(p[0]), dict(p[1])) for p in rules_raw
            if len(p) == _RULE_PAIR_LEN and isinstance(p[1], dict)
        )
    return TenantSpec(
        name=str(payload["name"]),
        vertex_labels=tuple(payload.get("vertex_labels") or []),
        edge_labels=tuple(payload.get("edge_labels") or []),
        indexes=tuple(payload.get("indexes") or []),
        generated_columns=tuple(payload.get("generated_columns") or []),
        label_hierarchy=hierarchy_pairs,
        resolve_rules=rules_pairs,
        meta=dict(payload.get("meta") or {}),
    )


async def persist_spec(pool: asyncpg.Pool, spec: TenantSpec) -> None:
    """Upsert the tenant spec into graphos_meta.tenants.

    Merge semantics (additive-by-default).  When a row already exists,
    the persisted spec is unioned with the incoming spec rather than
    blindly replaced — vertex_labels, edge_labels, indexes,
    generated_columns, label_hierarchy and resolve_rules become the
    set-union of existing and incoming; ``meta`` is merged dict-wise
    (incoming keys overwrite existing keys for the same name).

    Why: a blind ``DO UPDATE SET spec = EXCLUDED.spec`` would let any
    caller (even a bare ``{"name": ...}`` registration) shrink a tenant
    spec to empty labels, after which writes to labels created earlier
    fail with ``relation "<tenant>.<label>" does not exist``.

    To intentionally REMOVE a label (deprecation), use the explicit
    ``?force_shrink=true`` query param on ``POST /v1/tenants``,
    which calls ``persist_spec_replace`` below instead.
    """
    payload = json.dumps(_spec_to_jsonable(spec))
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO graphos_meta.tenants (name, spec)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (name)
            DO UPDATE SET
                spec = jsonb_build_object(
                    'name', COALESCE(EXCLUDED.spec->>'name', graphos_meta.tenants.spec->>'name'),
                    'vertex_labels',
                        (
                            SELECT COALESCE(to_jsonb(array_agg(DISTINCT x)), '[]'::jsonb)
                            FROM jsonb_array_elements_text(
                                COALESCE(graphos_meta.tenants.spec->'vertex_labels', '[]'::jsonb)
                                || COALESCE(EXCLUDED.spec->'vertex_labels', '[]'::jsonb)
                            ) AS x
                        ),
                    'edge_labels',
                        (
                            SELECT COALESCE(to_jsonb(array_agg(DISTINCT x)), '[]'::jsonb)
                            FROM jsonb_array_elements(
                                COALESCE(graphos_meta.tenants.spec->'edge_labels', '[]'::jsonb)
                                || COALESCE(EXCLUDED.spec->'edge_labels', '[]'::jsonb)
                            ) AS x
                        ),
                    'indexes',
                        (
                            SELECT COALESCE(to_jsonb(array_agg(DISTINCT x)), '[]'::jsonb)
                            FROM jsonb_array_elements_text(
                                COALESCE(graphos_meta.tenants.spec->'indexes', '[]'::jsonb)
                                || COALESCE(EXCLUDED.spec->'indexes', '[]'::jsonb)
                            ) AS x
                        ),
                    'generated_columns',
                        (
                            SELECT COALESCE(to_jsonb(array_agg(DISTINCT x)), '[]'::jsonb)
                            FROM jsonb_array_elements_text(
                                COALESCE(
                                    graphos_meta.tenants.spec->'generated_columns', '[]'::jsonb
                                )
                                || COALESCE(EXCLUDED.spec->'generated_columns', '[]'::jsonb)
                            ) AS x
                        ),
                    'label_hierarchy',
                        (
                            SELECT COALESCE(to_jsonb(array_agg(DISTINCT x)), '[]'::jsonb)
                            FROM jsonb_array_elements(
                                COALESCE(graphos_meta.tenants.spec->'label_hierarchy', '[]'::jsonb)
                                || COALESCE(EXCLUDED.spec->'label_hierarchy', '[]'::jsonb)
                            ) AS x
                        ),
                    'resolve_rules',
                        (
                            SELECT COALESCE(to_jsonb(array_agg(DISTINCT x)), '[]'::jsonb)
                            FROM jsonb_array_elements(
                                COALESCE(graphos_meta.tenants.spec->'resolve_rules', '[]'::jsonb)
                                || COALESCE(EXCLUDED.spec->'resolve_rules', '[]'::jsonb)
                            ) AS x
                        ),
                    'meta',
                        COALESCE(graphos_meta.tenants.spec->'meta', '{}'::jsonb)
                        || COALESCE(EXCLUDED.spec->'meta', '{}'::jsonb)
                ),
                updated_at = now();
            """,
            spec.name, payload,
        )


async def persist_spec_replace(pool: asyncpg.Pool, spec: TenantSpec) -> None:
    """Replace the persisted tenant spec wholesale (no merge).

    Reserved for the explicit ``?force_shrink=true`` admin path on
    ``POST /v1/tenants`` — used when an operator intentionally wants
    to remove labels from a tenant (e.g. deprecation).  Normal
    consumers should always go through :func:`persist_spec` so that
    a partial / bare registration cannot demote the schema.
    """
    payload = json.dumps(_spec_to_jsonable(spec))
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO graphos_meta.tenants (name, spec)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (name)
            DO UPDATE SET spec = EXCLUDED.spec, updated_at = now();
            """,
            spec.name, payload,
        )


async def load_registered_tenants(pool: asyncpg.Pool) -> int:
    """Populate the in-process registry from graphos_meta.tenants.

    Called from the FastAPI lifespan after the pool is ready. Idempotent;
    safe to call multiple times — the registry is overwritten each call.
    Returns the number of tenants restored.
    """
    async with pool.acquire() as conn:
        await allow_long_statements(conn, DDL_TIMEOUT_S)
        rows = await conn.fetch(
            "SELECT name, spec::text AS spec_text FROM graphos_meta.tenants;"
        )
    _REGISTERED.clear()
    for row in rows:
        try:
            payload = json.loads(row["spec_text"])
            _REGISTERED[row["name"]] = _spec_from_jsonable(payload)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning(
                "skipping corrupt tenant spec",
                extra={"tenant": row["name"], "error": str(exc)},
            )
    log.info("registered tenants restored from graphos_meta", extra={"count": len(_REGISTERED)})
    return len(_REGISTERED)


async def load_spec(pool: asyncpg.Pool, name: str) -> TenantSpec | None:
    """Return the persisted (merged) spec for ``name``, or None."""
    async with pool.acquire() as conn:
        text = await conn.fetchval(
            "SELECT spec::text FROM graphos_meta.tenants WHERE name = $1;", name,
        )
    return None if text is None else _spec_from_jsonable(json.loads(text))


async def register_tenant(pool: asyncpg.Pool, spec: TenantSpec) -> None:
    """Register a tenant — bring schema up, persist spec, remember in memory.

    Idempotent on all three levels: bootstrap is idempotent by construction,
    persistence is `ON CONFLICT DO UPDATE`, in-memory remember is a simple
    dict overwrite.
    """
    await bootstrap_tenant(pool, spec)
    await persist_spec(pool, spec)
    # Persisting merges with the stored spec; serve the merged result, not
    # the (possibly partial) incoming one.
    remember(await load_spec(pool, spec.name) or spec)


async def unregister_tenant(pool: asyncpg.Pool, name: str) -> bool:
    """Forget a tenant — drop the in-memory entry and the persistent row.

    Does **not** drop the AGE graph itself; that's a destructive operation
    that we don't want a `unregister` to imply. Use the data-plane DELETE
    endpoints first if you want to tombstone state. Returns True iff the
    name was registered.
    """
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM graphos_meta.tenants WHERE name = $1;", name,
        )
    existed = _REGISTERED.pop(name, None) is not None
    # asyncpg returns 'DELETE N'; we also count the in-memory removal as success.
    return existed or "DELETE 1" in result

"""Tenant bootstrap routine.

graphos is **tenant-agnostic**. There are no hardcoded tenant specs; consumers
register their own context at runtime via ``POST /v1/tenants``. The
registration payload carries the full schema (vertex labels, edge labels,
declared property indexes and generated columns, …), which this module turns
into idempotent SQL against the AGE-extended postgres.

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
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any, cast

import asyncpg

from graphos import by_entry, declared, node_properties, search
from graphos.db import DDL_TIMEOUT_S, allow_long_statements
from graphos.lookup import REV

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
        property_indexes:   Declared indexes on vertex-label properties
                            (``PropertyIndexSpec`` dumps); graphos writes the
                            DDL (``graphos.declared``).
        generated_columns:  Declared stored columns computed from a property
                            (``GeneratedColumnSpec`` dumps).
        meta:               Free-form consumer-supplied metadata; surfaced on
                            ``GET /v1/tenants``.
        edge_endpoints:     ``(edge_type, src_label, dst_label)`` pairs an edge
                            type is declared to join (``graphos.endpoints``).
                            Stored in ``graphos_meta.edge_endpoints``, not in
                            the spec JSON.
        strict_endpoints:   Refuse writes of a declared type outside its pairs.
                            ``None`` on an incoming spec: leave as stored.
                            Stored in ``graphos_meta.tenant_settings``.
    """

    name: str
    vertex_labels: tuple[str, ...] = ()
    edge_labels: tuple[str, ...] = ()
    property_indexes: tuple[dict[str, object], ...] = ()
    generated_columns: tuple[dict[str, object], ...] = ()
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
    # Named search indexes (``graphos.search``): name -> {labels, properties,
    # analyzer, language}.
    search_indexes: dict[str, dict[str, object]] = field(default_factory=dict)
    # How the web UI draws labels and edge types: ``{"vertex": {label: {color,
    # caption, size_by, icon}}, "edge": {type: {color, caption}}}``.  Cosmetic.
    display: dict[str, dict[str, dict[str, object]]] = field(default_factory=dict)
    # Declared properties per label / edge type (``*`` for every one) and the
    # mode writes are checked in: ``{"mode": …, "vertex": {label: entry},
    # "edge": {type: entry}}``, entries without nulls (graphos.properties).
    property_schema: dict[str, Any] = field(default_factory=dict)
    # What is on its way out, per vertex label and edge type (``graphos.renames``):
    # ``{"vertex": {label: {deprecated, replaced_by, note, properties}}, "edge": …}``.
    deprecated: dict[str, dict[str, dict[str, object]]] = field(default_factory=dict)
    # Every rename, oldest first (``graphos.renames``); kept by every registration.
    renames: tuple[dict[str, object], ...] = ()
    edge_endpoints: tuple[tuple[str, str, str], ...] = ()
    strict_endpoints: bool | None = None
    # The community algorithm a recompute uses when its request names none
    # (ADR-0011): "louvain" or "leiden". Always set once stored ("louvain" by
    # default); ``None`` on an incoming spec: leave as stored.
    community_algorithm: str | None = None


DEFAULT_COMMUNITY_ALGORITHM = "louvain"

# The stored spec's community algorithm: the one sent, else the one stored,
# else the default; never null (``$2`` is the incoming spec).
_COMMUNITY_ALGORITHM_SQL = (
    "COALESCE(NULLIF($2::jsonb->'community_algorithm', 'null'::jsonb), "
    "graphos_meta.tenants.spec->'community_algorithm', '\"louvain\"'::jsonb)"
)
# A new tenant's spec, with its community algorithm set.
_NEW_SPEC_SQL = (
    "jsonb_set($2::jsonb, '{community_algorithm}', "
    "COALESCE(NULLIF($2::jsonb->'community_algorithm', 'null'::jsonb), '\"louvain\"'::jsonb))"
)

# Spec fields stored in their own tables rather than in ``tenants.spec``.
_KEPT_APART = ("edge_endpoints", "strict_endpoints")


# ── In-process registry ─────────────────────────────────────────────────────

# Holds the currently-registered specs for this graphos-api process.
# Populated by ``register_tenant()``; read by the GET endpoint.
_REGISTERED: dict[str, TenantSpec] = {}

# Every change to ``graphos_meta.tenants`` is announced on this channel, in
# the same transaction, so other replicas reload it (``graphos.sync``).
TENANTS_CHANNEL = "graphos_tenants"


def registered_tenants() -> dict[str, TenantSpec]:
    return dict(_REGISTERED)


def remember(spec: TenantSpec) -> None:
    _REGISTERED[spec.name] = spec


def forget(name: str) -> bool:
    """Drop ``name`` from this process's registry.  True if it was there."""
    return _REGISTERED.pop(name, None) is not None


def spec_to_json(spec: TenantSpec) -> str:
    """A spec as stored in ``graphos_meta.tenants.spec`` (JSON text)."""
    return json.dumps(_spec_to_jsonable(spec))


def spec_from_json(text: str) -> TenantSpec:
    """A spec as stored in ``graphos_meta.tenants.spec`` (JSON text)."""
    return _spec_from_jsonable(json.loads(text))


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


async def _ensure_index_concurrently(
    conn: asyncpg.Connection, schema: str, table: str, name: str, definition: str,
    *, unique: bool = False,
) -> None:
    """``CREATE INDEX CONCURRENTLY IF NOT EXISTS``, redoing an interrupted build.

    Concurrent builds do not block writes, so an index can be added to a
    large existing table at startup.  A build that was interrupted leaves an
    invalid index that ``IF NOT EXISTS`` would keep skipping; it is dropped
    and built again.  Must run outside a transaction block.
    """
    valid = await conn.fetchval(
        "SELECT i.indisvalid FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relname = $2;",
        schema, name,
    )
    if valid:
        return
    if valid is False:
        await conn.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{schema}"."{name}";')
    started = time.perf_counter()
    unique_sql = "UNIQUE " if unique else ""
    await conn.execute(
        f'CREATE {unique_sql}INDEX CONCURRENTLY IF NOT EXISTS "{name}" '
        f'ON "{schema}"."{table}" {definition};'
    )
    took = time.perf_counter() - started
    if took > 1.0:  # noqa: PLR2004 — only builds over existing data are worth a line
        log.info("index built", extra={"tenant": schema, "index": name, "seconds": round(took, 1)})


# ── Bootstrap routine ───────────────────────────────────────────────────────


async def ensure_search_indexes(pool: asyncpg.Pool, spec: TenantSpec) -> None:
    """Build any Postgres index the spec's search indexes still lack.

    Bootstrap does this too; registration also calls it with the merged spec,
    so an index over every label (``*``) covers labels added since.
    """
    async with pool.acquire() as conn:
        await allow_long_statements(conn, DDL_TIMEOUT_S)
        await search.ensure_indexes(
            conn, spec.name, spec.search_indexes, _ensure_index_concurrently,
        )


async def _vertex_label_tables(conn: asyncpg.Connection, tenant: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_inherits i ON i.inhrelid = c.oid "
        "JOIN pg_class p ON p.oid = i.inhparent "
        "WHERE n.nspname = $1 AND p.relname = '_ag_label_vertex';",
        tenant,
    )
    return {str(r["relname"]) for r in rows}


# AGE 1.8 puts a statement-level trigger on every label table it creates, so
# writes made in plain SQL (graphos's) invalidate its in-memory graph, the one
# variable-length patterns read. Its 1.7.0 -> 1.8.0 upgrade script adds it to
# the label tables that already exist; a dump of an AGE 1.6 database restored
# into AGE 1.8 (docs/guide/upgrade-postgresql-18.md) runs no upgrade script, so its
# tables come without it. The same trigger as AGE's, where missing. The
# function exists from AGE 1.8 on; before that there is nothing to install.
CACHE_TRIGGER = "_age_cache_invalidate"
CACHE_TRIGGER_FUNCTION = "ag_catalog.age_invalidate_graph_cache()"

_LABEL_TABLES_WITHOUT_CACHE_TRIGGER_SQL = f"""
SELECT n.nspname, c.relname
  FROM ag_catalog.ag_label l
  JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
  JOIN pg_catalog.pg_class c ON c.oid = l.relation
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE g.name = $1
   AND l.name NOT IN ('_ag_label_vertex', '_ag_label_edge')
   AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger t
                    WHERE t.tgrelid = c.oid AND t.tgname = '{CACHE_TRIGGER}')
 ORDER BY c.relname;
"""


async def ensure_cache_invalidation_triggers(conn: asyncpg.Connection, tenant: str) -> int:
    """Add AGE 1.8's cache-invalidation trigger to ``tenant``'s label tables lacking it.

    Returns the number of tables it was added to: 0 on AGE before 1.8, and on
    every graph AGE 1.8 created (one catalogue read, then nothing). A table it
    cannot be added to (another role owns it) is logged and skipped: nothing
    in graphos reads AGE's in-memory graph, so it must not keep a tenant from
    starting.
    """
    if not await conn.fetchval(
        f"SELECT to_regprocedure('{CACHE_TRIGGER_FUNCTION}') IS NOT NULL;",
    ):
        return 0
    added = 0
    for row in await conn.fetch(_LABEL_TABLES_WITHOUT_CACHE_TRIGGER_SQL, tenant):
        schema, table = row["nspname"], row["relname"]
        try:
            await conn.execute(
                f'CREATE TRIGGER {CACHE_TRIGGER} '
                f'AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON "{schema}"."{table}" '
                f'FOR EACH STATEMENT EXECUTE FUNCTION {CACHE_TRIGGER_FUNCTION};'
            )
            added += 1
        except asyncpg.DuplicateObjectError:
            pass  # added meanwhile by another replica
        except asyncpg.PostgresError as exc:
            log.warning(
                "AGE cache-invalidation trigger not added",
                extra={"tenant": tenant, "table": table, "error": str(exc)},
            )
    if added:
        log.info(
            "AGE cache-invalidation trigger added to label tables",
            extra={"tenant": tenant, "tables": added},
        )
    return added


async def _ensure_generated_columns(conn: asyncpg.Connection, spec: TenantSpec) -> None:
    """Add each declared generated column its label table lacks.

    Raises:
        ValueError: a label that is not a vertex label of the tenant.
    """
    if not spec.generated_columns:
        return
    labels = await _vertex_label_tables(conn, spec.name)
    for col in spec.generated_columns:
        label = str(col["label"])
        if label not in labels:
            raise ValueError(f"generated column {col['column']!r}: {label!r} is not a vertex label")
        await _add_column_if_missing(
            conn, spec.name, label, str(col["column"]), declared.column_definition(col),
        )


async def _ensure_property_indexes(conn: asyncpg.Connection, spec: TenantSpec) -> None:
    """Build each declared property index the tenant lacks, concurrently.

    Raises:
        ValueError: a label that is not a vertex label of the tenant, nodes
            that already share the values of a unique index, or a value too
            long for a btree.
    """
    if not spec.property_indexes:
        return
    labels = await _vertex_label_tables(conn, spec.name)
    for idx in spec.property_indexes:
        label, name = str(idx["label"]), declared.index_name(idx)
        what = f"property index {declared.describe_index(idx)}"
        if label not in labels:
            raise ValueError(f"{what}: {label!r} is not a vertex label")
        try:
            await _ensure_index_concurrently(
                conn, spec.name, label, name, declared.index_definition(idx),
                unique=bool(idx.get("unique")),
            )
        except (asyncpg.UniqueViolationError, asyncpg.ProgramLimitExceededError) as exc:
            # The failed concurrent build leaves an invalid index behind.
            await conn.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{spec.name}"."{name}";')
            if isinstance(exc, asyncpg.UniqueViolationError):
                raise ValueError(f"{what}: nodes of {label!r} already share these values") from exc
            raise ValueError(
                f"{what}: a value is too long for an equality index "
                "(use kind 'trigram' for long text)"
            ) from exc


async def _add_column_if_missing(
    conn: asyncpg.Connection, schema: str, table: str, column: str, definition: str,
) -> None:
    """``ALTER TABLE … ADD COLUMN`` only when the column is missing.

    ``ADD COLUMN IF NOT EXISTS`` still takes an ACCESS EXCLUSIVE lock on the
    table and all its children, which every restart would then queue behind
    running queries; checking the catalog first takes none.
    """
    exists = await conn.fetchval(
        "SELECT 1 FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relname = $2 AND a.attname = $3 AND NOT a.attisdropped;",
        schema, table, column,
    )
    if not exists:
        await conn.execute(f'ALTER TABLE "{schema}"."{table}" ADD COLUMN {column} {definition};')


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


async def bootstrap_tenant(
    pool: asyncpg.Pool, spec: TenantSpec, *, vertex_graphid_indexes: bool = False,
) -> None:
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
            "property_indexes": len(spec.property_indexes),
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
            # 1. Graph itself.  A graph created here holds no nodes yet, so
            # its record of property names is complete from the start.
            existed = await conn.fetchval(
                "SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", spec.name,
            )
            await _safe(
                conn,
                f"SELECT create_graph('{spec.name}');",
                swallow=_SWALLOW_DUPLICATE_OBJECT,
            )
            if not existed:
                await node_properties.mark_new(conn, spec.name)

            # 1b. The revision column (graphos.writes bumps it on every
            # write).  Added to AGE's two parent tables: Postgres adds it to
            # every existing label table through inheritance, and every label
            # created later inherits it, default and NOT NULL included.  A
            # constant default is metadata-only, so even a table of millions
            # of rows is not rewritten; existing rows read as revision 1.
            for parent in ("_ag_label_vertex", "_ag_label_edge"):
                await _add_column_if_missing(
                    conn, spec.name, parent, REV, "bigint NOT NULL DEFAULT 1",
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
                # (``graphos.writes.upsert_nodes``) — ``INSERT … ON CONFLICT
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
                # Any other hot property is the consumer's to declare in the
                # registration's ``property_indexes`` (see graphos.declared).
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
            # And the start vertex's: a scoped walk crossing an edge
            # backwards (``direction=in`` / ``both``) reaches its start.
            await _safe(
                conn,
                f'ALTER TABLE "{spec.name}"."_ag_label_edge" '
                f'ADD COLUMN IF NOT EXISTS start_community_id text;',
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
                # community partition.  Naively this requires a per-hop semi-
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
                # The start vertex's community likewise: a walk crossing the
                # edge backwards reaches its start, so a scoped walk checks
                # this column on those hops (see /expand).
                await _safe(
                    conn,
                    f'ALTER TABLE "{spec.name}"."{label}" '
                    f'ADD COLUMN IF NOT EXISTS start_community_id text;',
                    swallow=_SWALLOW_DUPLICATE_COLUMN,
                )
                await conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "{label}_start_comm_id" '
                    f'ON "{spec.name}"."{label}" USING btree '
                    f'(start_community_id) WHERE start_community_id IS NOT NULL;'
                )

            # 4. Label hierarchy (postgres-inheritance multi-label
            # simulation).  Must run AFTER both vertex labels exist;
            # ``_inherit_if_missing`` checks pg_inherits first so
            # re-bootstrap of an already-wired tenant is a no-op.
            for child, parent in spec.label_hierarchy:
                await _inherit_if_missing(conn, spec.name, child, parent)

            # 5. Declared generated columns, added only when missing (adding
            # one to a label that has rows rewrites its table).
            await _ensure_generated_columns(conn, spec)

            # 6. AGE 1.8's cache-invalidation trigger, on label tables that
            # lack it (restored from an AGE 1.6 dump).
            await ensure_cache_invalidation_triggers(conn, spec.name)

            # 7. A btree on ``id`` (the graphid), which AGE does not create.
            # Edge tables always: paging edges in graphid order
            # (``/edges:scan``) reads it instead of sorting the table.
            # Vertex tables on request (GRAPHOS_VERTEX_GRAPHID_INDEXES):
            # resolving edge endpoints by graphid then probes an index
            # instead of scanning every label table, but measured bulk
            # writes of vertices 20-30% slower (edges ~5%).
            for label in spec.edge_labels:
                await _ensure_index_concurrently(
                    conn, spec.name, label, f"{label}_graphid", "(id)",
                )
            if vertex_graphid_indexes:
                for label in spec.vertex_labels:
                    await _ensure_index_concurrently(
                        conn, spec.name, label, f"{label}_graphid", "(id)",
                    )

            # 7b. The Postgres indexes behind each search index, one per
            # (label, property), built without blocking writes.  An unknown
            # text-search language or label raises ValueError (422).
            await search.ensure_indexes(
                conn, spec.name, spec.search_indexes, _ensure_index_concurrently,
            )

            # 7c. Declared property indexes, built the same way.
            await _ensure_property_indexes(conn, spec)

            # 8. Statistics for label tables never analyzed.  Autovacuum only
            # analyzes a table after 50+ changed rows, so small and empty
            # label tables can stay unknown forever; the planner then
            # assumes ~10 pages each, and a query across 80 such tables is
            # costed as millions of rows.  Cheap: only those tables, once.
            for row in await conn.fetch(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = $1 AND c.relkind = 'r' AND c.reltuples < 0;",
                spec.name,
            ):
                await conn.execute(f'ANALYZE "{spec.name}"."{row["relname"]}";')


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
    Declared endpoints and settings are left out: they have their own tables.
    """
    data = asdict(spec)
    for key in _KEPT_APART:
        data.pop(key, None)
    return data


def _spec_from_jsonable(payload: dict[str, object]) -> TenantSpec:
    if declared.has_legacy(payload):
        # Stored before 0.6.0; load_registered_tenants also rewrites the row.
        payload, _ = declared.convert_legacy(payload)
    # ``label_hierarchy`` round-trips as a list of [child, parent]
    # pairs because asdict() on a tuple-of-tuples produces nested
    # lists in JSON.  Tolerate both shapes (dict or list-of-pairs)
    # for forward compat with the wire body that uses a dict.
    raw: Any = payload.get("label_hierarchy") or ()
    if isinstance(raw, dict):
        hierarchy_pairs = tuple((str(k), str(v)) for k, v in raw.items())
    else:
        hierarchy_pairs = tuple(
            (str(p[0]), str(p[1])) for p in raw
        )
    # ``resolve_rules`` round-trips as either a dict ``{edge_type:
    # rule_dict}`` (preferred wire shape) or a list of
    # ``[edge_type, rule_dict]`` pairs (asdict on tuple-of-tuples).
    rules_raw: Any = payload.get("resolve_rules") or ()
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
        vertex_labels=tuple(cast("list[str]", payload.get("vertex_labels") or [])),
        edge_labels=tuple(cast("list[str]", payload.get("edge_labels") or [])),
        property_indexes=tuple(
            dict(x) for x in cast("list[dict[str, object]]", payload.get("property_indexes") or [])
        ),
        generated_columns=tuple(
            dict(x) for x in cast("list[dict[str, object]]", payload.get("generated_columns") or [])
        ),
        label_hierarchy=hierarchy_pairs,
        resolve_rules=rules_pairs,
        meta=dict(cast("dict[str, str]", payload.get("meta") or {})),
        search_indexes=dict(
            cast("dict[str, dict[str, object]]", payload.get("search_indexes") or {})
        ),
        display=display_of(payload.get("display")),
        property_schema=property_schema_of(payload.get("property_schema")),
        deprecated=by_entry.parse(payload.get("deprecated")),
        renames=tuple(
            dict(x) for x in cast("list[dict[str, object]]", payload.get("renames") or [])
            if isinstance(x, dict)
        ),
        community_algorithm=str(payload.get("community_algorithm") or DEFAULT_COMMUNITY_ALGORITHM),
    )


def property_schema_of(raw: object) -> dict[str, Any]:
    """A stored ``property_schema``: its mode when set, and its entries."""
    mode = raw.get("mode") if isinstance(raw, dict) else None
    return {**({"mode": mode} if isinstance(mode, str) else {}), **by_entry.parse(raw)}


def display_of(raw: object) -> dict[str, dict[str, dict[str, object]]]:
    """A stored ``display``: its entries, without null keys."""
    return by_entry.parse(raw, lambda e: {k: v for k, v in e.items() if v is not None})


def validate_display(
    display: dict[str, dict[str, dict[str, object]]],
    edge_labels: set[str],
    vertex_labels: set[str],
) -> None:
    """Refuse display entries naming a label or an edge type the tenant lacks."""
    for label in display.get("vertex", {}):
        if label not in vertex_labels:
            raise ValueError(f"display: {label!r} is not a vertex label of the tenant")
    for edge_type in display.get("edge", {}):
        if edge_type not in edge_labels:
            raise ValueError(f"display: {edge_type!r} is not an edge label of the tenant")


def validate_edge_endpoints(
    endpoints: tuple[tuple[str, str, str], ...],
    edge_labels: set[str],
    vertex_labels: set[str],
) -> None:
    """Refuse declared pairs naming an edge type or a label the tenant lacks.

    ``edge_labels`` / ``vertex_labels``: what is registered plus what the
    request adds, so a registration may declare pairs alone.
    """
    for edge_type, src, dst in endpoints:
        if edge_type not in edge_labels:
            raise ValueError(
                f"edge_endpoints type {edge_type!r} is not an edge label of the tenant"
            )
        for label in (src, dst):
            if label not in vertex_labels:
                raise ValueError(
                    f"edge_endpoints {edge_type!r} names {label!r}, "
                    "not a vertex label of the tenant"
                )


async def _supersede_kept(conn: asyncpg.Connection, name: str) -> None:
    """A name is registered or kept unregistered, never both: a registration
    written supersedes a kept one.  (The API restores a kept registration, or
    refuses the name, before it gets here.)"""
    await conn.execute("DELETE FROM graphos_meta.unregistered_tenants WHERE name = $1;", name)


async def _persist_endpoints(
    conn: asyncpg.Connection, spec: TenantSpec, *, replace: bool,
) -> None:
    """Store the spec's declared pairs (added, or replacing the stored ones) and settings."""
    if replace:
        await conn.execute("DELETE FROM graphos_meta.edge_endpoints WHERE tenant = $1;", spec.name)
    if spec.edge_endpoints:
        await conn.executemany(
            """
            INSERT INTO graphos_meta.edge_endpoints (tenant, edge_type, src_label, dst_label)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT DO NOTHING;
            """,
            [(spec.name, t, s, d) for t, s, d in spec.edge_endpoints],
        )
    if spec.strict_endpoints is not None:
        await conn.execute(
            """
            INSERT INTO graphos_meta.tenant_settings (tenant, strict_endpoints)
            VALUES ($1, $2)
            ON CONFLICT (tenant) DO UPDATE
            SET strict_endpoints = EXCLUDED.strict_endpoints, updated_at = now();
            """,
            spec.name, spec.strict_endpoints,
        )


async def remove_edge_endpoint(
    pool: asyncpg.Pool, tenant: str, edge_type: str, src_label: str, dst_label: str,
) -> bool:
    """Remove one declared pair.  True if it was declared.  Data is not touched."""
    async with pool.acquire() as conn, conn.transaction():
        result: str = await conn.execute(
            """
            DELETE FROM graphos_meta.edge_endpoints
            WHERE tenant = $1 AND edge_type = $2 AND src_label = $3 AND dst_label = $4;
            """,
            tenant, edge_type, src_label, dst_label,
        )
        await conn.execute("SELECT pg_notify($1, $2);", TENANTS_CHANNEL, tenant)
    return result == "DELETE 1"


# The by-entry merges of ``persist_spec`` (parameters $4-$9: the removed entries).
_DISPLAY_MERGE = by_entry.sql("display", "$4", "$5")
_PROPERTY_SCHEMA_MERGE = by_entry.sql("property_schema", "$6", "$7")
_DEPRECATED_MERGE = by_entry.sql("deprecated", "$8", "$9")


async def persist_spec(
    pool: asyncpg.Pool,
    spec: TenantSpec,
    *,
    replace_endpoints: bool = False,
    meta_removed: tuple[str, ...] = (),
    display_removed: tuple[tuple[str, str], ...] = (),
    schema_removed: tuple[tuple[str, str], ...] = (),
    deprecated_removed: tuple[tuple[str, str], ...] = (),
) -> None:
    """Upsert the tenant spec into graphos_meta.tenants.

    Merge semantics (additive-by-default).  When a row already exists,
    the persisted spec is unioned with the incoming spec rather than
    blindly replaced — vertex_labels, edge_labels, property_indexes,
    generated_columns, label_hierarchy and resolve_rules become the
    set-union of existing and incoming; ``meta`` is merged dict-wise
    (incoming keys overwrite existing keys for the same name), and the keys in
    ``meta_removed`` (sent as ``null``) are removed; ``display`` and
    ``property_schema`` and ``deprecated`` are merged by entry, the ``(part,
    name)`` entries in ``display_removed`` / ``schema_removed`` /
    ``deprecated_removed`` removed, and the property mode replaced only when
    the request states one.  ``renames`` stays as stored: only a rename writes it.

    Why: a blind ``DO UPDATE SET spec = EXCLUDED.spec`` would let any
    caller (even a bare ``{"name": ...}`` registration) shrink a tenant
    spec to empty labels, after which writes to labels created earlier
    fail with ``relation "<tenant>.<label>" does not exist``.

    To intentionally REMOVE a label (deprecation), use the explicit
    ``?force_shrink=true`` query param on ``POST /v1/tenants``,
    which calls ``persist_spec_replace`` below instead.

    Declared endpoints are added to the stored ones (replaced only with
    ``replace_endpoints``), in the same transaction.
    """
    payload = json.dumps(_spec_to_jsonable(spec))
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO graphos_meta.tenants (name, spec)
            VALUES ($1, """ + _NEW_SPEC_SQL + """)
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
                    'property_indexes',
                        (
                            SELECT COALESCE(to_jsonb(array_agg(DISTINCT x)), '[]'::jsonb)
                            FROM jsonb_array_elements(
                                COALESCE(graphos_meta.tenants.spec->'property_indexes', '[]'::jsonb)
                                || COALESCE(EXCLUDED.spec->'property_indexes', '[]'::jsonb)
                            ) AS x
                        ),
                    -- Declarations only: statements from before 0.6.0 are
                    -- converted when the server starts (declared.convert_legacy).
                    'generated_columns',
                        (
                            SELECT COALESCE(to_jsonb(array_agg(DISTINCT x)), '[]'::jsonb)
                            FROM jsonb_array_elements(
                                COALESCE(
                                    graphos_meta.tenants.spec->'generated_columns', '[]'::jsonb
                                )
                                || COALESCE(EXCLUDED.spec->'generated_columns', '[]'::jsonb)
                            ) AS x
                            WHERE jsonb_typeof(x) = 'object'
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
                        (COALESCE(graphos_meta.tenants.spec->'meta', '{}'::jsonb)
                         || COALESCE(EXCLUDED.spec->'meta', '{}'::jsonb)) - $3::text[],
                    -- By name: an index declared again replaces its definition.
                    'search_indexes',
                        COALESCE(graphos_meta.tenants.spec->'search_indexes', '{}'::jsonb)
                        || COALESCE(EXCLUDED.spec->'search_indexes', '{}'::jsonb),
                    -- By entry (graphos.by_entry); the property mode only when stated.
                    'display', jsonb_strip_nulls(jsonb_build_object(""" + _DISPLAY_MERGE + """)),
                    'property_schema', jsonb_strip_nulls(jsonb_build_object(
                        'mode', COALESCE(
                            EXCLUDED.spec #> '{property_schema,mode}',
                            graphos_meta.tenants.spec #> '{property_schema,mode}'),
                        """ + _PROPERTY_SCHEMA_MERGE + """)),
                    'deprecated', jsonb_strip_nulls(jsonb_build_object(
                        """ + _DEPRECATED_MERGE + """)),
                    'renames', COALESCE(graphos_meta.tenants.spec->'renames', '[]'::jsonb),
                    'community_algorithm', """ + _COMMUNITY_ALGORITHM_SQL + """
                ),
                updated_at = now();
            """,
            spec.name, payload, list(meta_removed),
            by_entry.names(display_removed, "vertex"), by_entry.names(display_removed, "edge"),
            by_entry.names(schema_removed, "vertex"), by_entry.names(schema_removed, "edge"),
            by_entry.names(deprecated_removed, "vertex"),
            by_entry.names(deprecated_removed, "edge"),
        )
        await _supersede_kept(conn, spec.name)
        await _persist_endpoints(conn, spec, replace=replace_endpoints)
        await conn.execute("SELECT pg_notify($1, $2);", TENANTS_CHANNEL, spec.name)


async def persist_spec_replace(
    pool: asyncpg.Pool, spec: TenantSpec, *, replace_endpoints: bool = False,
) -> None:
    """Replace the persisted tenant spec wholesale (no merge).

    Reserved for the explicit ``?force_shrink=true`` admin path on
    ``POST /v1/tenants`` — used when an operator intentionally wants
    to remove labels from a tenant (e.g. deprecation).  Normal
    consumers should always go through :func:`persist_spec` so that
    a partial / bare registration cannot demote the schema.  The
    ``renames`` history is kept: a replacement does not rewrite the past.
    """
    payload = json.dumps(_spec_to_jsonable(spec))
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO graphos_meta.tenants (name, spec)
            VALUES ($1, """ + _NEW_SPEC_SQL + """)
            ON CONFLICT (name)
            DO UPDATE SET spec = EXCLUDED.spec || jsonb_build_object(
                'renames', COALESCE(graphos_meta.tenants.spec->'renames', '[]'::jsonb),
                'community_algorithm', """ + _COMMUNITY_ALGORITHM_SQL + """),
                updated_at = now();
            """,
            spec.name, payload,
        )
        await _supersede_kept(conn, spec.name)
        await _persist_endpoints(conn, spec, replace=replace_endpoints)
        await conn.execute("SELECT pg_notify($1, $2);", TENANTS_CHANNEL, spec.name)


# A tenant's stored registration: the spec, its declared endpoints and
# settings, and a version that changes when any of them does (replicas reload
# a tenant when its version changes, ``graphos.sync``).
TENANT_ROWS_SQL = """
SELECT t.name,
       t.spec::text AS spec,
       COALESCE(e.pairs, '[]') AS endpoints,
       COALESCE(s.strict_endpoints, false) AS strict_endpoints,
       md5(t.spec::text || '|' || COALESCE(e.pairs, '')
           || '|' || COALESCE(s.strict_endpoints::text, '')) AS version
FROM graphos_meta.tenants t
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_array(edge_type, src_label, dst_label)
                     ORDER BY edge_type, src_label, dst_label)::text AS pairs
    FROM graphos_meta.edge_endpoints
    WHERE tenant = t.name
) e ON true
LEFT JOIN graphos_meta.tenant_settings s ON s.tenant = t.name
"""


def spec_from_row(row: Mapping[str, Any]) -> TenantSpec:
    """A tenant's spec from a ``TENANT_ROWS_SQL`` row, endpoints included."""
    spec = _spec_from_jsonable(json.loads(row["spec"]))
    endpoints = tuple(
        (str(t), str(s), str(d)) for t, s, d in json.loads(row["endpoints"] or "[]")
    )
    return replace(spec, edge_endpoints=endpoints, strict_endpoints=bool(row["strict_endpoints"]))


async def _convert_legacy_specs(
    conn: asyncpg.Connection, rows: list[asyncpg.Record],
) -> None:
    """Rewrite specs stored before 0.6.0, whose indexes and generated columns
    were SQL statements, with declarations instead (``declared.convert_legacy``).

    What the statements created stays in Postgres; a statement no declaration
    expresses is left out of the spec and logged, so an operator can see it.
    """
    for row in rows:
        try:
            payload = json.loads(row["spec"])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or not declared.has_legacy(payload):
            continue
        converted, dropped = declared.convert_legacy(payload)
        await conn.execute(
            "UPDATE graphos_meta.tenants SET spec = $2::jsonb, updated_at = now() "
            "WHERE name = $1;",
            row["name"], json.dumps(converted),
        )
        log.info(
            "tenant spec converted to declared indexes and columns",
            extra={
                "tenant": row["name"],
                "property_indexes": len(converted["property_indexes"]),
                "generated_columns": len(converted["generated_columns"]),
                "left_out": len(dropped),
            },
        )
        for stmt in dropped:
            log.warning(
                "tenant spec statement left out: no declaration says the same; what it "
                "created stays in Postgres",
                extra={"tenant": row["name"], "statement": stmt},
            )


async def load_registered_tenants(pool: asyncpg.Pool) -> int:
    """Populate the in-process registry from graphos_meta.tenants.

    Called from the FastAPI lifespan after the pool is ready. Idempotent;
    safe to call multiple times — the registry is overwritten each call.
    Returns the number of tenants restored.
    """
    async with pool.acquire() as conn:
        await allow_long_statements(conn, DDL_TIMEOUT_S)
        rows = await conn.fetch(TENANT_ROWS_SQL + ";")
        await _convert_legacy_specs(conn, rows)
        rows = await conn.fetch(TENANT_ROWS_SQL + ";")
    _REGISTERED.clear()
    for row in rows:
        try:
            _REGISTERED[row["name"]] = spec_from_row(row)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning(
                "skipping corrupt tenant spec",
                extra={"tenant": row["name"], "error": str(exc)},
            )
    log.info("registered tenants restored from graphos_meta", extra={"count": len(_REGISTERED)})
    return len(_REGISTERED)


async def load_spec(pool: asyncpg.Pool, name: str) -> TenantSpec | None:
    """Return the persisted (merged) spec for ``name``, or None."""
    found = await load_spec_versioned(pool, name)
    return None if found is None else found[0]


async def load_spec_versioned(pool: asyncpg.Pool, name: str) -> tuple[TenantSpec, str] | None:
    """The persisted spec for ``name`` and its version, read together, or None.

    The version is the one ``graphos.schema_version`` serves as the ETag: read
    from the same row, so it describes exactly this spec.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(TENANT_ROWS_SQL + " WHERE t.name = $1;", name)
    return None if row is None else (spec_from_row(row), str(row["version"]))


async def register_tenant(
    pool: asyncpg.Pool, spec: TenantSpec, *, vertex_graphid_indexes: bool = False,
) -> None:
    """Register a tenant — bring schema up, persist spec, remember in memory.

    Idempotent on all three levels: bootstrap is idempotent by construction,
    persistence is `ON CONFLICT DO UPDATE`, in-memory remember is a simple
    dict overwrite.
    """
    await bootstrap_tenant(pool, spec, vertex_graphid_indexes=vertex_graphid_indexes)
    await persist_spec(pool, spec)
    # Persisting merges with the stored spec; serve the merged result, not
    # the (possibly partial) incoming one.
    remember(await load_spec(pool, spec.name) or spec)


async def unregister_tenant(pool: asyncpg.Pool, name: str, *, by: str | None = None) -> bool:
    """Unregister a tenant, keeping its data and, to restore it, its registration.

    The registration (spec, declared endpoints, settings) moves to
    ``graphos_meta.unregistered_tenants`` in one transaction, so
    :func:`graphos.lifecycle.restore_tenant` brings the tenant back as it was.
    The AGE graph and its data stay; :func:`graphos.lifecycle.drop_tenant_data`
    deletes them.  Returns True iff the name was registered.
    """
    async with pool.acquire() as conn, conn.transaction():
        moved = await conn.fetchval(_UNREGISTER_SQL, name, by)
        # Its declared endpoints and settings went with the registration.
        await conn.execute("DELETE FROM graphos_meta.edge_endpoints WHERE tenant = $1;", name)
        await conn.execute("DELETE FROM graphos_meta.tenant_settings WHERE tenant = $1;", name)
        await conn.execute("SELECT pg_notify($1, $2);", TENANTS_CHANNEL, name)
    existed = forget(name)
    return existed or bool(moved)


# Moves a tenant's registration to ``unregistered_tenants`` (1 when it did).
_UNREGISTER_SQL = """
WITH gone AS (
    DELETE FROM graphos_meta.tenants WHERE name = $1 RETURNING name, spec, created_at
)
INSERT INTO graphos_meta.unregistered_tenants AS u
    (name, spec, endpoints, strict_endpoints, created_at, unregistered_by)
SELECT g.name, g.spec,
       COALESCE((SELECT jsonb_agg(jsonb_build_array(edge_type, src_label, dst_label)
                                  ORDER BY edge_type, src_label, dst_label)
                 FROM graphos_meta.edge_endpoints WHERE tenant = $1), '[]'::jsonb),
       COALESCE((SELECT strict_endpoints FROM graphos_meta.tenant_settings
                 WHERE tenant = $1), false),
       g.created_at, $2
FROM gone g
ON CONFLICT (name) DO UPDATE
SET spec = EXCLUDED.spec, endpoints = EXCLUDED.endpoints,
    strict_endpoints = EXCLUDED.strict_endpoints, created_at = EXCLUDED.created_at,
    unregistered_at = now(), unregistered_by = EXCLUDED.unregistered_by
RETURNING 1;
"""

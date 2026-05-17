"""Cypher execution helper.

Every data-plane endpoint goes through this module. Responsibilities:

- Take a Cypher template + a tenant name + a Python dict of parameters.
- Wrap them in AGE's ``cypher('<graph>', $$ … $$, <params>::agtype)`` form.
- Pass parameters as a single JSONB-cast-to-agtype value (AGE's only
  parameter-binding mechanism).
- Return rows as ordinary Python dicts. AGE returns each column as
  ``agtype`` text; this module converts back to ints, strings, dicts, lists.

agtype value shapes (observed against AGE 1.6.0):

    int:        "12345"
    bigint id:  "844424930131969"
    string:     '"foo"'
    bool:       "true" / "false"
    null:       "null"
    list:       "[1, 2, 3]"
    map:        '{"name": "foo"}'
    annotated:  '12345::numeric'  or  '{...}::vertex'  or  '{...}::edge'

We strip the optional ``::type`` annotation and JSON-decode the rest.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

log = logging.getLogger(__name__)


# Strip the trailing "::vertex" / "::edge" / "::numeric" annotation that AGE
# appends to typed values.
_AGTYPE_ANNOTATION = re.compile(r"::(\w+)$")


def parse_agtype(raw: Any) -> Any:
    """Convert an agtype scalar (string) into a native Python value.

    Returns the input untouched for non-string types — asyncpg has already
    decoded them. Returns ``None`` for the literal ``"null"`` string.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    stripped = _AGTYPE_ANNOTATION.sub("", raw).strip()
    if stripped == "null":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # Bare identifier (rare) — return verbatim.
        return stripped


class CypherError(RuntimeError):
    """Wraps an asyncpg error from a Cypher query with the query text for triage."""

    def __init__(self, message: str, *, sqlstate: str | None = None, query: str = ""):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.query = query


async def query_cypher(
    pool: asyncpg.Pool,
    *,
    graph: str,
    cypher: str,
    return_cols: Sequence[tuple[str, str]],
    params: Mapping[str, Any] | None = None,
    timeout_s: float | None = None,
) -> list[dict[str, Any]]:
    """Run a Cypher query against ``graph`` and return rows as Python dicts.

    Args:
        pool:        the asyncpg pool from app.state.
        graph:       AGE graph name (a registered tenant).
        cypher:      Cypher query body. Reference parameters as ``$name``.
        return_cols: list of ``(column_name, agtype_alias)`` pairs. The alias
                     is currently always ``agtype`` — kept as a parameter so
                     future return-type tightening (e.g. ``bigint``) is a
                     one-line change.
        params:      parameters passed to Cypher as ``$<name>``. Bound as a
                     single ``agtype`` map.
        timeout_s:   optional per-statement timeout.

    Returns:
        List of dicts keyed by column name. Each value is the parsed
        Python equivalent of the agtype output.

    Raises:
        CypherError: wraps any postgres error so callers don't have to
                     import asyncpg.
    """
    cols_decl = ", ".join(f"{name} {alias}" for name, alias in return_cols)
    sql = (
        f"SELECT * FROM cypher('{graph}', $$ {cypher} $$, $1::agtype) "
        f"AS ({cols_decl})"
    )
    params_json = json.dumps(params or {})

    async with pool.acquire() as conn:
        if timeout_s is not None:
            await conn.execute(
                f"SET LOCAL statement_timeout = '{int(timeout_s * 1000)}ms'"
            )
        try:
            rows = await conn.fetch(sql, params_json)
        except asyncpg.exceptions.QueryCanceledError:
            raise   # statement timeout → 504, not a 400
        except asyncpg.exceptions.PostgresError as exc:
            log.warning(
                "cypher query failed",
                extra={"graph": graph, "sqlstate": exc.sqlstate, "error": str(exc)},
            )
            raise CypherError(
                str(exc),
                sqlstate=exc.sqlstate,
                query=cypher.strip(),
            ) from exc

    out: list[dict[str, Any]] = []
    col_names = [name for name, _ in return_cols]
    for row in rows:
        out.append({name: parse_agtype(row[name]) for name in col_names})
    return out


async def execute_many(
    pool: asyncpg.Pool,
    *,
    graph: str,
    cypher: str,
    rows: Sequence[Mapping[str, Any]],
    timeout_s: float | None = None,
) -> int:
    """Run the same Cypher template N times within one transaction.

    Each row supplies the parameters for one execution. Used for batched
    Cypher where the *parameters* differ per row but the template is
    constant — works for shapes AGE supports parameterising (the MERGE
    pattern's match keys, RETURN expressions, …) but **not** for
    ``SET n += $param`` (see ``execute_many_with_inline_props``).

    The whole batch runs under one ``BEGIN/COMMIT``: partial failure
    rolls back the lot.
    """
    sql = f"SELECT * FROM cypher('{graph}', $$ {cypher} $$, $1::agtype) AS (_ agtype)"

    async with pool.acquire() as conn, conn.transaction():
        if timeout_s is not None:
            await conn.execute(
                f"SET LOCAL statement_timeout = '{int(timeout_s * 1000)}ms'"
            )
        try:
            for row in rows:
                await conn.execute(sql, json.dumps(row))
        except asyncpg.exceptions.QueryCanceledError:
            raise   # statement timeout → 504, not a 400
        except asyncpg.exceptions.PostgresError as exc:
            log.warning(
                "cypher batch failed",
                extra={"graph": graph, "sqlstate": exc.sqlstate, "error": str(exc)},
            )
            raise CypherError(
                str(exc),
                sqlstate=exc.sqlstate,
                query=cypher.strip(),
            ) from exc

    return len(rows)


# ── Cypher literal serialisation ────────────────────────────────────────────
#
# AGE 1.6 only accepts ``SET n += {literal_map}`` — not ``SET n += $param``
# (despite the openCypher spec). For upsert paths we serialise the property
# map into a Cypher literal so each row's MERGE+SET works without UNWIND.

_CYPHER_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def cypher_value_literal(v: Any) -> str:
    """Serialise a Python value as a Cypher literal.

    Supported: None, bool, int, float, str, list, dict (recursive).
    Strings are wrapped in single quotes with `'` and `\\` escaped.
    Raises TypeError for unsupported types — callers should validate
    payloads upstream (Pydantic).
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    if isinstance(v, list | tuple):
        return "[" + ", ".join(cypher_value_literal(x) for x in v) + "]"
    if isinstance(v, dict):
        return cypher_map_literal(v)
    raise TypeError(f"unsupported cypher value type: {type(v).__name__}")


def cypher_map_literal(d: Mapping[str, Any]) -> str:
    """Serialise a dict as a Cypher map literal, e.g. ``{name: 'foo', n: 3}``.

    Keys that are not plain identifiers are wrapped in backticks. Keys
    cannot contain backticks themselves; AGE rejects that.
    """
    parts: list[str] = []
    for k, v in d.items():
        if "`" in k:
            raise ValueError(f"property key may not contain backticks: {k!r}")
        key = k if _CYPHER_IDENT.fullmatch(k) else f"`{k}`"
        parts.append(f"{key}: {cypher_value_literal(v)}")
    return "{" + ", ".join(parts) + "}"


def _rows_written(status: str, *, default: int) -> int:
    """Rows an ``INSERT ... ON CONFLICT`` wrote, from its ``INSERT 0 <n>`` tag.

    Duplicate ids within one batch collapse into a single row, so this is the
    number of distinct rows inserted or updated, not the number of input rows.
    """
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return default


async def upsert_edges_inline(
    pool: asyncpg.Pool,
    *,
    graph: str,
    src_label: str,
    dst_label: str,
    edge_type: str,
    rows: Sequence[Mapping[str, Any]],
    timeout_s: float | None = None,
) -> tuple[int, int]:
    """Idempotent edge upsert keyed on ``(src_id, dst_id, edge_type)``.

    Every edge write goes through the SQL path in
    :func:`bulk_upsert_edges_via_copy` (``INSERT ... ON CONFLICT (start_id,
    end_id) DO UPDATE``), never through Cypher ``MERGE ... SET r += {...}``.

    Why: bootstrap adds an ``end_community_id`` column to every edge table
    (community-scoped traversal).  With that extra column present, AGE 1.6
    segfaults the Postgres backend on any Cypher ``SET`` that writes an edge
    row (new or existing) -- which restarts the whole server and drops every
    connection.  Plain SQL never goes through AGE's tuple-building code, so it
    is unaffected, and it keeps the same contract: properties merge
    additively, and rows whose endpoint does not exist are counted as skipped.

    Args:
        rows: each entry is ``{"src_id": str, "dst_id": str, "props": dict}``.

    Returns:
        ``(merged, skipped)``: distinct edges written, and input rows whose
        endpoint does not exist.
    """
    return await bulk_upsert_edges_via_copy(
        pool,
        graph=graph,
        src_label=src_label,
        dst_label=dst_label,
        edge_type=edge_type,
        rows=rows,
        timeout_s=timeout_s,
    )


async def bulk_upsert_nodes_via_copy(
    pool: asyncpg.Pool,
    *,
    graph: str,
    label: str,
    rows: Sequence[Mapping[str, Any]],
    timeout_s: float | None = None,
) -> int:
    """COPY-staged bulk upsert for nodes — replaces per-row ``MERGE`` cypher.

    Idempotency contract is identical to :func:`upsert_nodes_inline`:
    given the same external ``id``, properties merge into the existing
    row (Cypher ``SET n += {…}`` semantics) rather than duplicate.

    Flow per call:

    1. Open one asyncpg transaction.
    2. ``CREATE TEMP TABLE _stg_n (ext_id text, props jsonb) ON COMMIT DROP``.
    3. ``conn.copy_records_to_table('_stg_n', records=…)`` — asyncpg's
       binary COPY protocol, ~1 M rows/s on localhost.  No per-row
       cypher parse/plan, no agtype round-trip on the hot path.
    4. ``INSERT INTO "<graph>"."<Label>" (properties) SELECT
       jsonb_set(props, '{id}', to_jsonb(ext_id))::text::agtype …
       ON CONFLICT ((properties->'"id"')) DO UPDATE SET properties =
       OLD.properties || EXCLUDED.properties``.

       The UNIQUE expression index on ``((properties->'"id"'))`` is
       installed at bootstrap time (see ``bootstrap.py``).  AGE's
       graphid PK auto-generates via the column default — no manual
       ``nextval`` needed.

       The ``||`` operator on the agtype-cast-from-jsonb performs the
       same key-wise merge that Cypher's ``+=`` provides on the
       per-row path; keys present in the new row overwrite, keys
       only in the existing row survive.

    5. ``COMMIT`` — temp table drops automatically.

    Why this is faster than the per-row MERGE path: AGE's cypher()
    invokes the openCypher parser + planner per call, and each
    ``MATCH (n:Label {id: $id})`` resolves to a GIN containment scan
    even with the GIN index in place.  Direct INSERT skips parse +
    plan + agtype binding round-trip and rides Postgres's native
    INSERT-ON-CONFLICT path against the UNIQUE expression index.

    AGE Cypher visibility: ``MATCH`` against direct-inserted rows
    works because AGE reads the same per-label storage table.
    Verified end-to-end against AGE 1.6 (probe script in commit msg).

    Args:
        rows: same shape as :func:`upsert_nodes_inline` — each entry
              is ``{"id": <str>, "props": <dict>}``.

    Returns:
        Number of rows processed (the cardinality of ``rows``).  We
        don't distinguish insert vs. update — ``MERGE`` semantics are
        the contract, and the caller has no way to use the split.
    """
    if not rows:
        return 0

    records = [
        (str(row["id"]), json.dumps(row.get("props", {})))
        for row in rows
    ]

    async with pool.acquire() as conn, conn.transaction():
        if timeout_s is not None:
            await conn.execute(
                f"SET LOCAL statement_timeout = '{int(timeout_s * 1000)}ms'"
            )
        try:
            # ``synchronous_commit = off`` skips the per-commit fsync.  Safe
            # for bulk writes: every write is an idempotent upsert
            # (idempotent (every batch carries the full property bag, ON
            # CONFLICT merges), so a crash between commit-ack and on-disk
            # WAL flush just means the caller's next attempt re-applies the
            # same rows.  ``SET LOCAL`` scopes to this tx only -- no impact
            # on parallel read connections or future writes.  Field benefit:
            # 2-3x throughput on burst-write workloads where commit fsync
            # dominates (verified Postgres community benchmark).
            await conn.execute("SET LOCAL synchronous_commit = off;")
            await conn.execute(
                "CREATE TEMP TABLE _stg_n (ext_id text, props jsonb) "
                "ON COMMIT DROP;"
            )
            await conn.copy_records_to_table(
                "_stg_n", records=records, columns=("ext_id", "props"),
            )
            # jsonb_set fills the canonical ``id`` field; if the
            # caller already supplied id in props it gets overwritten
            # so the persisted ``properties->'"id"'`` always equals
            # the routing ext_id.
            #
            # Intra-batch dedup: a caller can legitimately send the same
            # ext_id more than once in one request (e.g. the same document
            # produced by two sources).  Postgres ``ON CONFLICT DO UPDATE`` rejects
            # duplicates within a single INSERT with "cannot affect
            # row a second time", so we collapse staging to one row
            # per ext_id BEFORE the INSERT.
            #
            # ``DISTINCT ON (ext_id) … ORDER BY ext_id, ctid DESC``
            # keeps the LAST physical staging row per ext_id -- same
            # last-wins semantic Cypher's per-row ``MERGE … SET n +=``
            # would yield if the consumer issued the same writes
            # serially.  ON CONFLICT below still handles the
            # cross-batch / cross-call dedup against existing rows
            # by merging their property bags.
            sql = (
                f'INSERT INTO "{graph}"."{label}" (properties) '
                f"SELECT (jsonb_set(props, '{{id}}', to_jsonb(ext_id)))"
                f"::text::agtype "
                f"FROM ("
                f"  SELECT DISTINCT ON (ext_id) ext_id, props "
                f"    FROM _stg_n "
                f"   ORDER BY ext_id, ctid DESC"
                f") deduped "
                f"ON CONFLICT ((properties->'\"id\"')) "
                f'DO UPDATE SET properties = '
                f'"{label}".properties || EXCLUDED.properties;'
            )
            status = await conn.execute(sql)
        except asyncpg.exceptions.QueryCanceledError:
            raise   # statement timeout → 504, not a 400
        except asyncpg.exceptions.PostgresError as exc:
            log.warning(
                "bulk node COPY upsert failed",
                extra={"graph": graph, "label": label, "sqlstate": exc.sqlstate},
            )
            raise CypherError(
                str(exc),
                sqlstate=exc.sqlstate,
                query=f"COPY+INSERT ON CONFLICT :{label}",
            ) from exc

    return _rows_written(status, default=len(rows))


async def bulk_upsert_edges_via_copy(
    pool: asyncpg.Pool,
    *,
    graph: str,
    src_label: str,
    dst_label: str,
    edge_type: str,
    rows: Sequence[Mapping[str, Any]],
    timeout_s: float | None = None,
) -> tuple[int, int]:
    """COPY-staged bulk upsert for edges — replaces per-row MERGE cypher.

    Mirrors :func:`upsert_edges_inline` semantics: missing-endpoint
    rows (either ``src_id`` or ``dst_id`` not present in the vertex
    tables) are counted in ``skipped`` and silently dropped — same
    behaviour the per-row MATCH-MATCH-MERGE path provides.

    Flow per call:

    1. ``CREATE TEMP TABLE _stg_e`` with (src_ext, dst_ext, props).
    2. ``conn.copy_records_to_table('_stg_e', …)`` — binary COPY.
    3. JOIN against the src/dst label tables to resolve external ids
       to AGE graphids; INSERT-SELECT into the edge label table with
       ``ON CONFLICT (start_id, end_id)`` for idempotency.
    4. Skipped count = staging rows whose LEFT JOIN missed either
       endpoint.

    The UNIQUE constraint on ``(start_id, end_id)`` is installed at
    bootstrap time alongside the plain ``(start_id, end_id)`` btree used by reads.  The
    text-to-agtype cast for the JOIN comparison uses
    ``to_jsonb(text)::text::agtype`` — verified against AGE 1.6 to
    yield the same agtype scalar the cypher write path stores.

    Args:
        rows: same shape as :func:`upsert_edges_inline` — each entry
              is ``{"src_id": str, "dst_id": str, "props": dict}``.

    Returns:
        ``(merged, skipped)``: distinct edges written, and input rows whose
        endpoint does not exist.
    """
    if not rows:
        return 0, 0

    records = [
        (str(row["src_id"]), str(row["dst_id"]), json.dumps(row.get("props", {})))
        for row in rows
    ]

    async with pool.acquire() as conn, conn.transaction():
        if timeout_s is not None:
            await conn.execute(
                f"SET LOCAL statement_timeout = '{int(timeout_s * 1000)}ms'"
            )
        try:
            # See ``bulk_upsert_nodes_via_copy`` for the safety argument.
            # Edges carry the same idempotency contract -- a re-apply of
            # the same (src_id, dst_id) pair under the UNIQUE constraint
            # is a property-merge on the existing row.
            await conn.execute("SET LOCAL synchronous_commit = off;")
            # Force Nested Loop + Index Scan for the JOIN against the
            # vertex label tables.  Without this Postgres picks Hash
            # Join and seq-scans the entire src/dst label table to
            # build the hash side -- O(N) over the label tables --
            # which becomes the dominant cost at 100k+-row scale.  The
            # ``UNIQUE INDEX ((properties->'"id"'::agtype))`` we
            # install at bootstrap can serve the JOIN as cheap btree
            # probes (5000 stg rows × log2(100k) ≈ 85k probes ≪ one
            # 100k-row seq-scan), but the planner only picks it when
            # hash isn't an option.  ``SET LOCAL`` scopes the hint to
            # this transaction only -- read paths keep their default
            # planner behaviour.  Field benefit: edge-write CPU on
            # graphos-db drops from ~140% to a fraction of that, and
            # per-batch INSERT time falls from seconds to ~100 ms.
            await conn.execute("SET LOCAL enable_hashjoin = off;")
            # Bigger work_mem helps the per-batch sort/dedup in the
            # ``DISTINCT ON (src_ext, dst_ext)`` CTE on large batches
            # (10k+ rows).
            await conn.execute("SET LOCAL work_mem = '256MB';")
            await conn.execute(
                "CREATE TEMP TABLE _stg_e "
                "(src_ext text, dst_ext text, props jsonb) "
                "ON COMMIT DROP;"
            )
            await conn.copy_records_to_table(
                "_stg_e",
                records=records,
                columns=("src_ext", "dst_ext", "props"),
            )
            # Skipped count — count staging rows where at least one
            # endpoint vertex doesn't exist.  Rewritten as ``NOT
            # EXISTS`` (anti-semi-join) rather than ``LEFT JOIN …
            # WHERE … IS NULL`` so the planner uses Index-Only Scan
            # via the UNIQUE expression index even with
            # ``enable_hashjoin = off`` on the INSERT side -- the
            # LEFT JOIN form forced a Hash-Anti-Join over the full
            # label table, undoing the planner hint's benefit and
            # turning a sub-second probe into a multi-second scan
            # at 100k+-vertex scale.
            skipped_row = await conn.fetchrow(
                f"SELECT count(*) AS skipped FROM _stg_e stg "
                f"WHERE NOT EXISTS ("
                f'  SELECT 1 FROM "{graph}"."{src_label}" src '
                f"    WHERE src.properties->'\"id\"' "
                f"        = to_jsonb(stg.src_ext)::text::agtype"
                f") OR NOT EXISTS ("
                f'  SELECT 1 FROM "{graph}"."{dst_label}" dst '
                f"    WHERE dst.properties->'\"id\"' "
                f"        = to_jsonb(stg.dst_ext)::text::agtype"
                f");"
            )
            skipped = int(skipped_row["skipped"])

            # Intra-batch dedup on (src_ext, dst_ext) -- same reason
            # as the node path: duplicate (src, dst) pairs within a
            # single INSERT would trip "cannot affect row a second
            # time".  Last-wins via DISTINCT ON, then resolve
            # graphids via JOIN against the vertex tables.
            sql = (
                f'INSERT INTO "{graph}"."{edge_type}" '
                f"(start_id, end_id, properties) "
                f"SELECT src.id, dst.id, (stg.props)::text::agtype "
                f"FROM ("
                f"  SELECT DISTINCT ON (src_ext, dst_ext) "
                f"         src_ext, dst_ext, props "
                f"    FROM _stg_e "
                f"   ORDER BY src_ext, dst_ext, ctid DESC"
                f") stg "
                f'JOIN "{graph}"."{src_label}" src '
                f"  ON src.properties->'\"id\"' "
                f"   = to_jsonb(stg.src_ext)::text::agtype "
                f'JOIN "{graph}"."{dst_label}" dst '
                f"  ON dst.properties->'\"id\"' "
                f"   = to_jsonb(stg.dst_ext)::text::agtype "
                f"ON CONFLICT (start_id, end_id) "
                f"DO UPDATE SET properties = "
                f'"{edge_type}".properties || EXCLUDED.properties;'
            )
            status = await conn.execute(sql)
        except asyncpg.exceptions.QueryCanceledError:
            raise   # statement timeout → 504, not a 400
        except asyncpg.exceptions.PostgresError as exc:
            log.warning(
                "bulk edge COPY upsert failed",
                extra={
                    "graph": graph,
                    "src_label": src_label,
                    "dst_label": dst_label,
                    "edge_type": edge_type,
                    "sqlstate": exc.sqlstate,
                },
            )
            raise CypherError(
                str(exc),
                sqlstate=exc.sqlstate,
                query=(
                    f"COPY+INSERT ON CONFLICT "
                    f"-[{src_label}-{edge_type}->{dst_label}]-"
                ),
            ) from exc

    return _rows_written(status, default=len(rows) - skipped), skipped


async def upsert_nodes_inline(
    pool: asyncpg.Pool,
    *,
    graph: str,
    label: str,
    rows: Sequence[Mapping[str, Any]],
    timeout_s: float | None = None,
) -> int:
    """Idempotent node upsert keyed on the external ``id``.

    Delegates to :func:`bulk_upsert_nodes_via_copy` (``INSERT ... ON CONFLICT
    ((properties->'"id"')) DO UPDATE``): graphos writes no rows through Cypher.

    Why: AGE 1.6 segfaults the Postgres backend when a Cypher ``SET`` writes a
    row into a label table that has columns beyond AGE's own -- which is what
    registration ``generated_columns`` add (and what bootstrap adds to edge
    tables).  The crash restarts the whole server.  The SQL path never goes
    through AGE's tuple-building code and keeps the same contract: properties
    merge additively, keys absent from the new payload are preserved.

    Args:
        rows: each entry is ``{"id": <str>, "props": <dict>}``.

    Returns:
        Number of rows applied.
    """
    return await bulk_upsert_nodes_via_copy(
        pool, graph=graph, label=label, rows=rows, timeout_s=timeout_s,
    )

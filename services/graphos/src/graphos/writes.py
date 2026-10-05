"""Every write to a tenant's graph data goes through this module.

Nodes, edges, property merges and patches, deletes, rule-resolved edges
and community write-back: each function here is the only place its SQL
lives.  Nothing
else in graphos inserts, updates or deletes rows of the AGE label tables
(``tests/test_write_path.py`` enforces it).  Revisions and a change log,
when they come, are added here once instead of at every call site.

Conventions every function follows:

* **One transaction per call**, owned by the function.  The one exception
  is :func:`set_edge_end_communities`, which commits each edge label
  separately so a failure on one label keeps the others.
* **One node or edge by identity** (merge, patch, single-edge delete) is
  found with :mod:`graphos.lookup` inside the write's transaction, its row
  locked, so an id in several labels raises
  :class:`graphos.lookup.AmbiguousMatchError` instead of writing them all.
* **Names are trusted.** Tenant, label and edge-type names are
  interpolated into SQL; callers pass names already checked against the
  tenant's registered spec.  Values always go in as bind parameters.
* **Errors:** a statement timeout propagates as asyncpg's
  ``QueryCanceledError`` (the API maps it to 504); any other database error
  is raised as :class:`graphos.cypher.CypherError` (its name predates the
  move to SQL) and logged.

All writes are SQL, never Cypher.  Bootstrap adds an ``end_community_id``
column to every edge table and registration may add generated columns;
AGE 1.6 segfaults the Postgres backend on any Cypher ``SET`` / ``MERGE``
that writes a row into a label table with extra columns, which restarts
the whole server.  Plain SQL never goes through AGE's tuple-building code.

Writes to graphos's own metadata (``graphos_meta.tenants`` / ``cache``)
and DDL are not graph data and stay with their owners (``bootstrap``,
``api/cache``).
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import asyncpg

from graphos import lookup
from graphos.cypher import CypherError
from graphos.db import allow_long_statements

if TYPE_CHECKING:
    from graphos.resolver import CompiledEdgeType

log = logging.getLogger(__name__)


@contextlib.contextmanager
def _db_errors(message: str, *, query: str, **context: Any) -> Iterator[None]:
    """Re-raise database errors as :class:`CypherError`, timeouts untouched."""
    try:
        yield
    except asyncpg.exceptions.QueryCanceledError:
        raise
    except asyncpg.exceptions.PostgresError as exc:
        log.warning(message, extra={**context, "sqlstate": exc.sqlstate})
        raise CypherError(str(exc), sqlstate=exc.sqlstate, query=query) from exc


def _rows_written(status: str, *, default: int) -> int:
    """Row count from a command tag such as ``INSERT 0 <n>`` / ``UPDATE <n>``.

    Duplicate ids within one batch collapse into a single row, so for an
    upsert this is the number of distinct rows inserted or updated, not the
    number of input rows.
    """
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return default


async def _set_timeout(conn: asyncpg.Connection, timeout_s: float | None) -> None:
    if timeout_s is not None:
        await conn.execute(f"SET LOCAL statement_timeout = '{int(timeout_s * 1000)}ms'")


# ── Nodes and edges ─────────────────────────────────────────────────────────


async def upsert_nodes(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    label: str,
    rows: Sequence[Mapping[str, Any]],
    timeout_s: float | None = None,
) -> int:
    """Idempotent node upsert keyed on the external ``id``.

    Rows are COPYed into a temp staging table, then one ``INSERT ... ON
    CONFLICT ((properties->'"id"')) DO UPDATE SET properties = old || new``
    against the per-label unique index installed at bootstrap.  Properties
    merge additively (openCypher ``SET n += map``): keys absent from the new
    payload are preserved.  Within one batch the last row for an id wins.

    Args:
        rows: each entry is ``{"id": <str>, "props": <dict>}``.

    Returns:
        Distinct nodes inserted or updated.
    """
    if not rows:
        return 0

    records = [(str(row["id"]), json.dumps(row.get("props", {}))) for row in rows]

    with _db_errors(
        "bulk node COPY upsert failed",
        query=f"COPY+INSERT ON CONFLICT :{label}", graph=tenant, label=label,
    ):
        async with pool.acquire() as conn, conn.transaction():
            await _set_timeout(conn, timeout_s)
            # Every write is an idempotent upsert carrying the full property
            # bag, so a crash between commit-ack and WAL flush only means the
            # caller's retry re-applies the same rows.  Scoped to this tx.
            await conn.execute("SET LOCAL synchronous_commit = off;")
            await conn.execute(
                "CREATE TEMP TABLE _stg_n (ext_id text, props jsonb) ON COMMIT DROP;"
            )
            await conn.copy_records_to_table(
                "_stg_n", records=records, columns=("ext_id", "props"),
            )
            # jsonb_set writes the routing id into the stored ``id`` property,
            # overriding any ``id`` the caller put in props.  ``ON CONFLICT DO
            # UPDATE`` refuses to touch a row twice in one statement, so the
            # batch is collapsed to the last staging row per id first.
            status = await conn.execute(
                f'INSERT INTO "{tenant}"."{label}" (properties) '
                f"SELECT (jsonb_set(props, '{{id}}', to_jsonb(ext_id)))::text::agtype "
                f"FROM ("
                f"  SELECT DISTINCT ON (ext_id) ext_id, props "
                f"    FROM _stg_n "
                f"   ORDER BY ext_id, ctid DESC"
                f") deduped "
                f"ON CONFLICT ((properties->'\"id\"')) "
                f'DO UPDATE SET properties = "{label}".properties || EXCLUDED.properties;'
            )

    return _rows_written(status, default=len(rows))


async def upsert_edges(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    src_label: str,
    dst_label: str,
    edge_type: str,
    rows: Sequence[Mapping[str, Any]],
    timeout_s: float | None = None,
) -> tuple[int, int]:
    """Idempotent edge upsert keyed on ``(src_id, dst_id, edge_type)``.

    Rows are COPYed into staging, joined to the src/dst label tables to turn
    external ids into graphids, and inserted with ``ON CONFLICT (start_id,
    end_id) DO UPDATE``: properties merge additively.  Rows whose endpoint
    does not exist are skipped and counted.

    Args:
        rows: each entry is ``{"src_id": str, "dst_id": str, "props": dict}``.

    Returns:
        ``(written, skipped)``: distinct edges inserted or updated, and input
        rows whose endpoint does not exist.
    """
    if not rows:
        return 0, 0

    records = [
        (str(row["src_id"]), str(row["dst_id"]), json.dumps(row.get("props", {})))
        for row in rows
    ]

    with _db_errors(
        "bulk edge COPY upsert failed",
        query=f"COPY+INSERT ON CONFLICT -[{src_label}-{edge_type}->{dst_label}]-",
        graph=tenant, src_label=src_label, dst_label=dst_label, edge_type=edge_type,
    ):
        async with pool.acquire() as conn, conn.transaction():
            await _set_timeout(conn, timeout_s)
            await conn.execute("SET LOCAL synchronous_commit = off;")
            # Nested loop + index probes on the per-label ``id`` unique index
            # instead of hash-joining whole label tables, which dominates at
            # 100k+ vertices.  work_mem covers the batch sort for DISTINCT ON.
            await conn.execute("SET LOCAL enable_hashjoin = off;")
            await conn.execute("SET LOCAL work_mem = '256MB';")
            await conn.execute(
                "CREATE TEMP TABLE _stg_e (src_ext text, dst_ext text, props jsonb) "
                "ON COMMIT DROP;"
            )
            await conn.copy_records_to_table(
                "_stg_e", records=records, columns=("src_ext", "dst_ext", "props"),
            )
            # NOT EXISTS (anti-semi-join) keeps index-only probes; a LEFT JOIN
            # ... IS NULL planned a hash anti-join over the whole label table.
            skipped = int(await conn.fetchval(
                f"SELECT count(*) FROM _stg_e stg "
                f"WHERE NOT EXISTS ("
                f'  SELECT 1 FROM "{tenant}"."{src_label}" src '
                f"   WHERE src.properties->'\"id\"' = to_jsonb(stg.src_ext)::text::agtype"
                f") OR NOT EXISTS ("
                f'  SELECT 1 FROM "{tenant}"."{dst_label}" dst '
                f"   WHERE dst.properties->'\"id\"' = to_jsonb(stg.dst_ext)::text::agtype"
                f");"
            ))
            status = await conn.execute(
                f'INSERT INTO "{tenant}"."{edge_type}" (start_id, end_id, properties) '
                f"SELECT src.id, dst.id, (stg.props)::text::agtype "
                f"FROM ("
                f"  SELECT DISTINCT ON (src_ext, dst_ext) src_ext, dst_ext, props "
                f"    FROM _stg_e "
                f"   ORDER BY src_ext, dst_ext, ctid DESC"
                f") stg "
                f'JOIN "{tenant}"."{src_label}" src '
                f"  ON src.properties->'\"id\"' = to_jsonb(stg.src_ext)::text::agtype "
                f'JOIN "{tenant}"."{dst_label}" dst '
                f"  ON dst.properties->'\"id\"' = to_jsonb(stg.dst_ext)::text::agtype "
                f"ON CONFLICT (start_id, end_id) "
                f'DO UPDATE SET properties = "{edge_type}".properties || EXCLUDED.properties;'
            )

    return _rows_written(status, default=len(rows) - skipped), skipped


async def merge_node_properties(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    external_id: str,
    properties: Mapping[str, Any],
    label: str | None = None,
) -> str | None:
    """Merge ``properties`` onto the node whose ``id`` is ``external_id``.

    Top-level keys in the payload overwrite, keys absent are preserved
    (agtype ``||``).  ``label`` picks the node when the id exists in more
    than one label.

    Returns:
        The label of the node updated, or ``None`` if no node has that id.

    Raises:
        graphos.lookup.AmbiguousMatchError: the id exists in several labels
            and no ``label`` was given.  Nothing is written.
    """
    with _db_errors(
        "node property merge failed", query="UPDATE <label> SET properties ||", graph=tenant,
    ):
        async with pool.acquire() as conn, conn.transaction():
            node = await lookup.find_node(
                conn, tenant=tenant, external_id=external_id, label=label, for_update=True,
            )
            if node is None:
                return None
            await conn.execute(
                f'UPDATE "{tenant}"."{node.label}" '
                f"SET properties = properties || $2::text::agtype WHERE {lookup.ID_MATCH};",
                json.dumps(external_id),
                json.dumps(dict(properties)),
            )
            return node.label


def apply_merge_patch(target: Any, patch: Any) -> Any:
    """``target`` with JSON Merge Patch ``patch`` applied (RFC 7396).

    An object patch merges key by key, recursing into nested objects; a
    ``None`` (JSON ``null``) value removes the key; any other value, arrays
    included, replaces it.  ``target`` is not modified.
    """
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = apply_merge_patch(result.get(key), value)
    return result


async def patch_node(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    external_id: str,
    patch: Mapping[str, Any],
    label: str | None = None,
) -> lookup.NodeRow | None:
    """Apply a JSON Merge Patch to the properties of one node.

    The node is read and locked, patched, and written back in one
    transaction.  ``label`` picks the node when the id exists in more than
    one label.

    Returns:
        The node as written, or ``None`` if no node has that id.

    Raises:
        ValueError: the patch touches ``id``, which names the node.
        graphos.lookup.AmbiguousMatchError: see :func:`merge_node_properties`.
    """
    if "id" in patch:
        raise ValueError("'id' names the node and cannot be patched")
    with _db_errors("node patch failed", query="UPDATE <label> SET properties", graph=tenant):
        async with pool.acquire() as conn, conn.transaction():
            node = await lookup.find_node(
                conn, tenant=tenant, external_id=external_id, label=label, for_update=True,
            )
            if node is None:
                return None
            properties = apply_merge_patch(node.properties, dict(patch))
            await conn.execute(
                f'UPDATE "{tenant}"."{node.label}" '
                f"SET properties = $2::text::agtype WHERE {lookup.ID_MATCH};",
                json.dumps(external_id),
                json.dumps(properties),
            )
    return lookup.NodeRow(label=node.label, properties=properties)


async def patch_edge(  # noqa: PLR0913 — the edge's identity has this many parts
    pool: asyncpg.Pool,
    *,
    tenant: str,
    edge_type: str,
    src_id: str,
    dst_id: str,
    patch: Mapping[str, Any],
    src_label: str | None = None,
    dst_label: str | None = None,
) -> lookup.EdgeRow | None:
    """Apply a JSON Merge Patch to the properties of one edge.

    Returns:
        The edge as written, or ``None`` if there is no such edge.

    Raises:
        graphos.lookup.AmbiguousMatchError: an end id exists in several
            labels, more than one such edge exists, and the labels given
            don't pick one.  Nothing is written.
    """
    with _db_errors(
        "edge patch failed", query=f"UPDATE {edge_type} SET properties", graph=tenant,
    ):
        async with pool.acquire() as conn, conn.transaction():
            edge = await lookup.find_edge(
                conn, tenant=tenant, edge_type=edge_type, src_id=src_id, dst_id=dst_id,
                src_label=src_label, dst_label=dst_label, for_update=True,
            )
            if edge is None:
                return None
            properties = apply_merge_patch(edge.properties, dict(patch))
            await conn.execute(
                f'UPDATE "{tenant}"."{edge_type}" SET properties = $3::text::agtype '
                f"WHERE start_id = $1::text::graphid AND end_id = $2::text::graphid;",
                edge.start_gid,
                edge.end_gid,
                json.dumps(properties),
            )
    return lookup.EdgeRow(
        src_label=edge.src_label, dst_label=edge.dst_label,
        start_gid=edge.start_gid, end_gid=edge.end_gid, properties=properties,
    )


async def delete_edge(  # noqa: PLR0913 — the edge's identity has this many parts
    pool: asyncpg.Pool,
    *,
    tenant: str,
    edge_type: str,
    src_id: str,
    dst_id: str,
    src_label: str | None = None,
    dst_label: str | None = None,
) -> int:
    """Delete one edge; its end nodes stay.

    Returns:
        1, or 0 if there is no such edge (idempotent).

    Raises:
        graphos.lookup.AmbiguousMatchError: see :func:`patch_edge`.
    """
    with _db_errors("edge delete failed", query=f"DELETE {edge_type}", graph=tenant):
        async with pool.acquire() as conn, conn.transaction():
            edge = await lookup.find_edge(
                conn, tenant=tenant, edge_type=edge_type, src_id=src_id, dst_id=dst_id,
                src_label=src_label, dst_label=dst_label, for_update=True,
            )
            if edge is None:
                return 0
            await conn.execute(
                f'DELETE FROM "{tenant}"."{edge_type}" '
                f"WHERE start_id = $1::text::graphid AND end_id = $2::text::graphid;",
                edge.start_gid,
                edge.end_gid,
            )
    return 1


async def delete_nodes(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    property_key: str,
    property_value: str,
    label: str | None = None,
) -> tuple[int, int]:
    """Delete every node whose ``property_key`` equals the string
    ``property_value`` (in ``label`` only, when given), with every edge that
    touches one of them.

    One statement, so nodes and edges go together or not at all.  ``id`` is
    matched through the per-label unique index; any other key through the
    per-label GIN index on ``properties`` (agtype containment, which matches
    exactly what Cypher ``n.key = 'value'`` matches: string values only, not
    arrays containing the value, not numbers).  ``property_key`` is
    interpolated, so the caller validates it as an identifier.

    Returns:
        ``(nodes_deleted, edges_deleted)``, each edge counted once.
    """
    if property_key == "id":
        match = "properties->'\"id\"' = $1::text::agtype"
        arg = json.dumps(property_value)
    else:
        match = "properties @> $1::text::agtype"
        arg = json.dumps({property_key: property_value})
    sql = f"""
        WITH v AS (
            DELETE FROM "{tenant}"."{label or '_ag_label_vertex'}"
            WHERE {match}
            RETURNING id
        ), e AS (
            DELETE FROM "{tenant}"."_ag_label_edge"
            WHERE start_id = ANY(ARRAY(SELECT id FROM v))
               OR end_id = ANY(ARRAY(SELECT id FROM v))
            RETURNING id
        )
        SELECT (SELECT count(*) FROM v) AS nodes, (SELECT count(*) FROM e) AS edges;
    """
    with _db_errors(
        "node delete failed", query=f"DELETE nodes WHERE {property_key} = …",
        graph=tenant, property_key=property_key,
    ):
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(sql, arg)
    return int(row["nodes"]), int(row["edges"])


# ── Rule-resolved edges ─────────────────────────────────────────────────────


async def insert_resolved_edges(
    pool: asyncpg.Pool,
    *,
    compiled: CompiledEdgeType,
    timeout_s: float,
) -> int:
    """Run the resolve ``INSERT`` compiled for one edge type.

    The statement (built by ``graphos.resolver.compiler``) inserts with
    ``ON CONFLICT (start_id, end_id) DO NOTHING`` and ``RETURNING`` one row
    per new edge.  It can cover the full cross-product of a label pair, so
    it runs with ``timeout_s`` instead of the pool's read-sized default.

    Returns:
        Edges written; 0 when no rule of this type matched any label.
    """
    if compiled.sql is None:
        return 0
    with _db_errors(
        "resolve INSERT failed", query=f"resolve INSERT :{compiled.edge_type}",
        edge_type=compiled.edge_type, arms=compiled.arm_count,
    ):
        async with pool.acquire() as conn, conn.transaction():
            await allow_long_statements(conn, timeout_s)
            rows = await conn.fetch(compiled.sql, *compiled.params, timeout=timeout_s)
    return len(rows)


# ── Community write-back ────────────────────────────────────────────────────


async def set_node_communities(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    assignments: Sequence[tuple[int, int]],
) -> None:
    """Set ``community_id`` on the given vertices.

    Args:
        assignments: ``(graphid, community_id)`` pairs; graphids as integers.
    """
    if not assignments:
        return
    with _db_errors(
        "community write-back failed", query="UPDATE _ag_label_vertex community_id",
        graph=tenant,
    ):
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL synchronous_commit = off;")
            await conn.execute(
                "CREATE TEMP TABLE _stg_assignments (gid bigint, community_id int) "
                "ON COMMIT DROP;"
            )
            await conn.copy_records_to_table(
                "_stg_assignments", records=assignments, columns=("gid", "community_id"),
            )
            await allow_long_statements(conn, 600.0)
            await conn.execute(
                f'UPDATE "{tenant}"."_ag_label_vertex" t '
                f"SET properties = t.properties || "
                f"('{{\"community_id\": ' || s.community_id || '}}')::agtype "
                f"FROM _stg_assignments s "
                f"WHERE t.id = (s.gid::text)::graphid;",
                timeout=600.0,
            )


async def set_edge_end_communities(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    assignments: Iterable[tuple[int, str]],
) -> int:
    """Backfill ``end_community_id`` onto every edge of *tenant*.

    The ``(graphid, community_id)`` pairs are COPYed once into a staging
    table, streamed from ``assignments`` (a generator is fine), then each
    edge label table gets one UPDATE joined to it.  ``IS DISTINCT FROM``
    skips rows whose value is unchanged.  Unlike the other writes, each
    per-label UPDATE commits on its own, so a failure on one label keeps
    the others.

    Returns:
        Edge rows updated across all labels.
    """
    total = 0
    with _db_errors(
        "edge community write-back failed", query="UPDATE edges end_community_id",
        graph=tenant,
    ):
        async with pool.acquire() as conn:
            await conn.execute(
                "CREATE TEMP TABLE _stg_edge_communities (gid bigint, cid text);"
            )
            try:
                await conn.copy_records_to_table(
                    "_stg_edge_communities", records=assignments, columns=("gid", "cid"),
                )
                # So each per-label plan sees the staging cardinality.
                await conn.execute("ANALYZE _stg_edge_communities;")
                label_rows = await conn.fetch(
                    "SELECT c.relname "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN pg_inherits i ON i.inhrelid = c.oid "
                    "JOIN pg_class p ON p.oid = i.inhparent "
                    "WHERE n.nspname = $1 "
                    "  AND p.relname = '_ag_label_edge' "
                    "  AND c.relkind = 'r' "
                    "ORDER BY c.relname;",
                    tenant,
                )
                for label in (str(r["relname"]) for r in label_rows):
                    await allow_long_statements(conn, 300.0)
                    updated = await conn.execute(
                        f'UPDATE "{tenant}"."{label}" e '
                        f"SET end_community_id = s.cid "
                        f"FROM _stg_edge_communities s "
                        f"WHERE e.end_id = (s.gid::text)::graphid "
                        f"  AND e.end_community_id IS DISTINCT FROM s.cid;",
                        timeout=300.0,
                    )
                    total += _rows_written(updated, default=0)
                    log.info(
                        "louvain.edge_writeback_label_done",
                        extra={"tenant": tenant, "label": label, "total_updated_so_far": total},
                    )
            finally:
                await conn.execute("DROP TABLE IF EXISTS _stg_edge_communities;")

    return total

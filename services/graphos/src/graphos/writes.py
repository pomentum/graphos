"""Every write to a tenant's graph data goes through this module.

Nodes, edges, property merges and patches, deletes, rule-resolved edges
and community write-back: each function here is the only place its SQL
lives.  Nothing
else in graphos inserts, updates or deletes rows of the AGE label tables
(``tests/test_write_path.py`` enforces it), so revisions and the change
feed are kept here once instead of at every call site.

Conventions every function follows:

* **One transaction per call**, owned by the function.  The one exception
  is :func:`set_edge_end_communities`, which commits each edge label
  separately so a failure on one label keeps the others.
* **Every write logs a change** with :func:`graphos.changes.record`, in
  the same transaction, so the change feed has a record exactly when the
  data commits (``tests/test_changes.py`` checks that every write function
  here records).
* **Every write to a row bumps its revision** (``graphos_rev``, on every
  label table): inserts start at 1, updates add 1.  A write that sets the
  same values still bumps it; the revision counts writes, not changes.
* **One node or edge by identity** (merge, patch, single delete) is found
  with :mod:`graphos.lookup` inside the write's transaction, its row
  locked, so an id in several labels raises
  :class:`graphos.lookup.AmbiguousMatchError` instead of writing them all,
  and an ``If-Match`` precondition (:class:`graphos.lookup.IfMatch`) is
  checked against the row it will write.
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

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import asyncpg

from graphos import changes, lookup, node_properties
from graphos.cypher import CypherError
from graphos.db import allow_long_statements

if TYPE_CHECKING:
    from graphos.community_map import CommunityMapData
    from graphos.resolver import CompiledEdgeType


# A check of the row a single write leaves, called under its lock before the
# update: ``(label or edge type, id, properties)``; it raises to refuse the
# write (``graphos.properties.checker``).
RowCheck = Callable[[str, str | None, Mapping[str, Any]], None]

log = logging.getLogger(__name__)


@contextlib.contextmanager
def _db_errors(message: str, *, query: str, **context: Any) -> Iterator[None]:
    """Re-raise database errors as :class:`CypherError`, timeouts untouched.

    A unique violation is left as it is too: it is a declared unique property
    index refusing a duplicate (the API answers 409), not a bad request.
    """
    try:
        yield
    except (asyncpg.exceptions.QueryCanceledError, asyncpg.exceptions.UniqueViolationError):
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


# SET clause that bumps the revision of the row being updated.
_BUMP = f"{lookup.REV} = {lookup.REV} + 1"


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
    names = {"id"}.union(*(row.get("props") or {} for row in rows))

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
            # The property names written are recorded by the same statement.
            status = await conn.execute(
                f"{node_properties.NOTE_CTE}"
                f'INSERT INTO "{tenant}"."{label}" (properties) '
                f"SELECT (jsonb_set(props, '{{id}}', to_jsonb(ext_id)))::text::agtype "
                f"FROM ("
                f"  SELECT DISTINCT ON (ext_id) ext_id, props "
                f"    FROM _stg_n "
                f"   ORDER BY ext_id, ctid DESC"
                f") deduped "
                f"ON CONFLICT ((properties->'\"id\"')) "
                f'DO UPDATE SET properties = "{label}".properties || EXCLUDED.properties, '
                f'{lookup.REV} = "{label}".{lookup.REV} + 1;',
                tenant, label, sorted(names),
            )
            written = _rows_written(status, default=len(rows))
            await changes.record(
                conn, tenant=tenant, op="upsert", kind="node", label=label,
                ids=list(dict.fromkeys(ext_id for ext_id, _ in records)), count=written,
            )

    return written


# The mark on the edges a resolve rule derived (ADR-0001): ``edges:resolve`` in
# reconcile mode removes only marked edges, and a consumer's write to an edge
# takes it over by clearing the mark, so neither removes the other's edges.
DERIVED = "graphos_derived"
_MARKED = "e.properties::text::jsonb @> '{\"graphos_derived\": true}'"


def _taken_over(edge_type: str) -> str:
    """An upsert's merged properties, without the rules' mark when there was one
    (the text test keeps the usual upsert free of jsonb casts)."""
    old = f'"{edge_type}".properties'
    return (
        f"CASE WHEN {old}::text LIKE '%{DERIVED}%' "
        f"THEN (({old} || EXCLUDED.properties)::text::jsonb - '{DERIVED}')::text::agtype "
        f"ELSE {old} || EXCLUDED.properties END"
    )


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
            # The pairs whose two ends exist: the edges the INSERT writes
            # (for the change record), and by difference the skipped rows.
            # EXISTS (semi-join) keeps index-only probes; a join planned a
            # hash join over the whole label table.
            present = {
                (r["src_ext"], r["dst_ext"]) for r in await conn.fetch(
                    f"SELECT DISTINCT src_ext, dst_ext FROM _stg_e stg "
                    f"WHERE EXISTS ("
                    f'  SELECT 1 FROM "{tenant}"."{src_label}" src '
                    f"   WHERE src.properties->'\"id\"' = to_jsonb(stg.src_ext)::text::agtype"
                    f") AND EXISTS ("
                    f'  SELECT 1 FROM "{tenant}"."{dst_label}" dst '
                    f"   WHERE dst.properties->'\"id\"' = to_jsonb(stg.dst_ext)::text::agtype"
                    f");"
                )
            }
            skipped = sum(1 for s, d, _ in records if (s, d) not in present)
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
                f"DO UPDATE SET properties = {_taken_over(edge_type)}, "
                f'{lookup.REV} = "{edge_type}".{lookup.REV} + 1;'
            )
            written = _rows_written(status, default=len(rows) - skipped)
            if written:
                pairs = dict.fromkeys((s, d) for s, d, _ in records if (s, d) in present)
                await changes.record(
                    conn, tenant=tenant, op="upsert", kind="edge", label=edge_type,
                    src_label=src_label, dst_label=dst_label,
                    ids=[list(p) for p in pairs], count=written,
                )

    return written, skipped


async def merge_node_properties(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    external_id: str,
    properties: Mapping[str, Any],
    label: str | None = None,
    if_match: lookup.IfMatch | None = None,
    check: RowCheck | None = None,
) -> lookup.NodeRow | None:
    """Merge ``properties`` onto the node whose ``id`` is ``external_id``.

    Top-level keys in the payload overwrite, keys absent are preserved
    (agtype ``||``).  ``label`` picks the node when the id exists in more
    than one label.

    Returns:
        The node as written, or ``None`` if no node has that id.

    Raises:
        graphos.lookup.AmbiguousMatchError: the id exists in several labels
            and no ``label`` was given.  Nothing is written.
        graphos.lookup.PreconditionFailedError: ``if_match`` does not hold.
    """
    with _db_errors(
        "node property merge failed", query="UPDATE <label> SET properties ||", graph=tenant,
    ):
        async with pool.acquire() as conn, conn.transaction():
            node = await _locked_node(conn, tenant, external_id, label, if_match)
            if node is None:
                return None
            if check is not None:
                check(node.label, external_id, {**node.properties, **properties})
            row = await conn.fetchrow(
                f'UPDATE ONLY "{tenant}"."{node.label}" '
                f"SET properties = properties || $2::text::agtype, {_BUMP} "
                f"WHERE {lookup.ID_MATCH} "
                f"RETURNING properties::text::jsonb AS props, {lookup.REV} AS rev;",
                json.dumps(external_id),
                json.dumps(dict(properties)),
            )
            await node_properties.note(conn, tenant, node.label, properties)
            await changes.record(
                conn, tenant=tenant, op="merge", kind="node", label=node.label,
                ids=[external_id], rev=int(row["rev"]), count=1,
            )
    return lookup.NodeRow(
        label=node.label, gid=node.gid, rev=int(row["rev"]), properties=json.loads(row["props"]),
    )


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


async def patch_node(  # noqa: PLR0913 — identity, patch and precondition
    pool: asyncpg.Pool,
    *,
    tenant: str,
    external_id: str,
    patch: Mapping[str, Any],
    label: str | None = None,
    if_match: lookup.IfMatch | None = None,
    check: RowCheck | None = None,
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
        graphos.lookup.PreconditionFailedError: ``if_match`` does not hold.
    """
    if "id" in patch:
        raise ValueError("'id' names the node and cannot be patched")
    with _db_errors("node patch failed", query="UPDATE <label> SET properties", graph=tenant):
        async with pool.acquire() as conn, conn.transaction():
            node = await _locked_node(conn, tenant, external_id, label, if_match)
            if node is None:
                return None
            properties = apply_merge_patch(node.properties, dict(patch))
            if check is not None:
                check(node.label, external_id, properties)
            rev = await conn.fetchval(
                f'UPDATE ONLY "{tenant}"."{node.label}" '
                f"SET properties = $2::text::agtype, {_BUMP} WHERE {lookup.ID_MATCH} "
                f"RETURNING {lookup.REV};",
                json.dumps(external_id),
                json.dumps(properties),
            )
            await node_properties.note(
                conn, tenant, node.label, (k for k, v in patch.items() if v is not None),
            )
            await changes.record(
                conn, tenant=tenant, op="patch", kind="node", label=node.label,
                ids=[external_id], rev=int(rev), count=1,
            )
    return lookup.NodeRow(label=node.label, gid=node.gid, rev=int(rev), properties=properties)


async def delete_node(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    external_id: str,
    label: str | None = None,
    if_match: lookup.IfMatch | None = None,
) -> tuple[int, int]:
    """Delete one node, found as :func:`graphos.lookup.find_node` finds it,
    with every edge touching it, in one transaction.

    Unlike :func:`delete_nodes`, an id in several labels is refused, so a
    precondition is checked against exactly one node.

    Returns:
        ``(nodes_deleted, edges_deleted)``; ``(0, 0)`` if there is no such
        node (and no ``if_match``).

    Raises:
        graphos.lookup.AmbiguousMatchError: see :func:`merge_node_properties`.
        graphos.lookup.PreconditionFailedError: ``if_match`` does not hold.
    """
    with _db_errors("node delete failed", query="DELETE <label> by id", graph=tenant):
        async with pool.acquire() as conn, conn.transaction():
            node = await _locked_node(conn, tenant, external_id, label, if_match)
            if node is None:
                return 0, 0
            return await _delete_nodes_recorded(
                conn, tenant,
                _DELETE_NODES_SQL.format(table=f'ONLY "{tenant}"."{node.label}"',
                                         tenant=tenant, match=lookup.ID_MATCH),
                json.dumps(external_id),
            )


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
    if_match: lookup.IfMatch | None = None,
    check: RowCheck | None = None,
) -> lookup.EdgeRow | None:
    """Apply a JSON Merge Patch to the properties of one edge.

    Returns:
        The edge as written, or ``None`` if there is no such edge.

    Raises:
        graphos.lookup.AmbiguousMatchError: an end id exists in several
            labels, more than one such edge exists, and the labels given
            don't pick one.  Nothing is written.
        graphos.lookup.PreconditionFailedError: ``if_match`` does not hold.
    """
    with _db_errors(
        "edge patch failed", query=f"UPDATE {edge_type} SET properties", graph=tenant,
    ):
        async with pool.acquire() as conn, conn.transaction():
            edge = await lookup.find_edge(
                conn, tenant=tenant, edge_type=edge_type, src_id=src_id, dst_id=dst_id,
                src_label=src_label, dst_label=dst_label, for_update=True,
            )
            if if_match is not None:
                if_match.check(edge.etag if edge else None)
            if edge is None:
                return None
            properties = apply_merge_patch(edge.properties, dict(patch))
            if isinstance(properties, dict):
                # A consumer's write takes a derived edge over (ADR-0001).
                properties.pop(DERIVED, None)
            if check is not None:
                check(edge_type, None, properties)
            rev = await conn.fetchval(
                f'UPDATE ONLY "{tenant}"."{edge_type}" '
                f"SET properties = $3::text::agtype, {_BUMP} "
                f"WHERE start_id = $1::text::graphid AND end_id = $2::text::graphid "
                f"RETURNING {lookup.REV};",
                edge.start_gid,
                edge.end_gid,
                json.dumps(properties),
            )
            await changes.record(
                conn, tenant=tenant, op="patch", kind="edge", label=edge_type,
                src_label=edge.src_label, dst_label=edge.dst_label,
                ids=[[src_id, dst_id]], rev=int(rev), count=1,
            )
    return lookup.EdgeRow(
        src_label=edge.src_label, dst_label=edge.dst_label, gid=edge.gid,
        start_gid=edge.start_gid, end_gid=edge.end_gid, rev=int(rev), properties=properties,
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
    if_match: lookup.IfMatch | None = None,
) -> int:
    """Delete one edge; its end nodes stay.

    Returns:
        1, or 0 if there is no such edge (idempotent without ``if_match``).

    Raises:
        graphos.lookup.AmbiguousMatchError: see :func:`patch_edge`.
        graphos.lookup.PreconditionFailedError: ``if_match`` does not hold.
    """
    with _db_errors("edge delete failed", query=f"DELETE {edge_type}", graph=tenant):
        async with pool.acquire() as conn, conn.transaction():
            edge = await lookup.find_edge(
                conn, tenant=tenant, edge_type=edge_type, src_id=src_id, dst_id=dst_id,
                src_label=src_label, dst_label=dst_label, for_update=True,
            )
            if if_match is not None:
                if_match.check(edge.etag if edge else None)
            if edge is None:
                return 0
            await conn.execute(
                f'DELETE FROM ONLY "{tenant}"."{edge_type}" '
                f"WHERE start_id = $1::text::graphid AND end_id = $2::text::graphid;",
                edge.start_gid,
                edge.end_gid,
            )
            await changes.record(
                conn, tenant=tenant, op="delete", kind="edge", label=edge_type,
                src_label=edge.src_label, dst_label=edge.dst_label,
                ids=[[src_id, dst_id]], count=1,
            )
    return 1


async def _locked_node(
    conn: asyncpg.Connection,
    tenant: str,
    external_id: str,
    label: str | None,
    if_match: lookup.IfMatch | None,
) -> lookup.NodeRow | None:
    """Find and lock one node, and check the caller's precondition on it."""
    node = await lookup.find_node(
        conn, tenant=tenant, external_id=external_id, label=label, for_update=True,
    )
    if if_match is not None:
        if_match.check(node.etag if node else None)
    return node


# One statement, so nodes and their edges go together or not at all.  One
# row per label of the deleted nodes, with their ids, for the change records.
_DELETE_NODES_SQL = """
    WITH v AS (
        DELETE FROM {table}
        WHERE {match}
        RETURNING id, tableoid, properties::text::jsonb -> 'id' AS ext
    ), e AS (
        DELETE FROM "{tenant}"."_ag_label_edge"
        WHERE start_id = ANY(ARRAY(SELECT id FROM v))
           OR end_id = ANY(ARRAY(SELECT id FROM v))
        RETURNING id
    )
    SELECT c.relname AS label, count(*) AS nodes,
           jsonb_agg(v.ext ORDER BY v.ext)::text AS ids,
           (SELECT count(*) FROM e) AS edges
    FROM v JOIN pg_class c ON c.oid = v.tableoid
    GROUP BY c.relname
    ORDER BY c.relname;
"""


async def _delete_nodes_recorded(
    conn: asyncpg.Connection, tenant: str, sql: str, arg: str | list[str],
    detail: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """Run a node delete and record one change per label it deleted from.

    ``edges_removed`` in each record is the count for the whole statement:
    every edge that touched any of the deleted nodes.
    """
    rows = await conn.fetch(sql, arg)
    edges = int(rows[0]["edges"]) if rows else 0
    for r in rows:
        await changes.record(
            conn, tenant=tenant, op="delete", kind="node", label=r["label"],
            ids=json.loads(r["ids"]), count=int(r["nodes"]),
            detail={"edges_removed": edges, **(detail or {})},
        )
    return sum(int(r["nodes"]) for r in rows), edges


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
        match = lookup.ID_MATCH
        arg = json.dumps(property_value)
    else:
        match = "properties @> $1::text::agtype"
        arg = json.dumps({property_key: property_value})
    sql = _DELETE_NODES_SQL.format(
        table=f'"{tenant}"."{label or "_ag_label_vertex"}"', tenant=tenant, match=match,
    )
    with _db_errors(
        "node delete failed", query=f"DELETE nodes WHERE {property_key} = …",
        graph=tenant, property_key=property_key,
    ):
        async with pool.acquire() as conn, conn.transaction():
            return await _delete_nodes_recorded(
                conn, tenant, sql, arg,
                None if property_key == "id" else {"where": {property_key: property_value}},
            )


async def delete_nodes_by_ref(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    refs: Iterable[tuple[str, str]],
) -> tuple[int, int]:
    """Delete the nodes named by ``(label, id)`` pairs, with every edge that
    touches one of them, in one transaction.

    One statement per label, its ids matched with ``= ANY(...)``, which the
    label's ``<Label>_id_unique`` index serves.  A label takes in its child
    labels' nodes too, as the single delete does.  A pair that names no node
    is skipped, so a repeat deletes nothing.  An edge between nodes of two
    labels goes with the first label's statement and is counted once;
    ``edges_removed`` in each label's change record is that statement's count.

    Two batches deleting nodes joined by edges can lock each other's edges
    (a deadlock, or a serialization failure): the whole transaction is rolled
    back and run again, up to :data:`DELETE_ATTEMPTS` times.

    Returns:
        ``(nodes_deleted, edges_deleted)``.
    """
    by_label: dict[str, set[str]] = {}
    for label, external_id in refs:
        by_label.setdefault(label, set()).add(external_id)
    with _db_errors("node delete failed", query="DELETE <label> by ids", graph=tenant):
        for attempt in range(1, DELETE_ATTEMPTS + 1):
            try:
                return await _delete_by_label(pool, tenant, by_label)
            except (asyncpg.exceptions.DeadlockDetectedError,
                    asyncpg.exceptions.SerializationError):
                if attempt == DELETE_ATTEMPTS:
                    raise
                await asyncio.sleep(0.05 * attempt)
    raise AssertionError("unreachable")


# Runs of a batch delete that lost a deadlock before the error is returned.
DELETE_ATTEMPTS = 3


async def _delete_by_label(
    pool: asyncpg.Pool, tenant: str, by_label: dict[str, set[str]],
) -> tuple[int, int]:
    """One transaction of :func:`delete_nodes_by_ref`."""
    nodes = edges = 0
    async with pool.acquire() as conn, conn.transaction():
        for label in sorted(by_label):
            n, e = await _delete_nodes_recorded(
                conn, tenant,
                _DELETE_NODES_SQL.format(table=f'"{tenant}"."{label}"',
                                         tenant=tenant, match=lookup.IDS_MATCH),
                sorted(json.dumps(i) for i in by_label[label]),
            )
            nodes += n
            edges += e
    return nodes, edges


# ── Rule-resolved edges ─────────────────────────────────────────────────────


async def insert_resolved_edges(
    pool: asyncpg.Pool,
    *,
    tenant: str,
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
            if rows:
                # Too many to list: consumers re-read the edge type.
                await changes.record(
                    conn, tenant=tenant, op="resolve", kind="edge",
                    label=compiled.edge_type, count=len(rows),
                )
    return len(rows)


# ── Reconciliation: make a stored edge set equal to a given one ─────────────
#
# ADR-0001.  The caller names a scope (one node's edges of a type towards a
# label, or every edge of a type between two labels) and sends the whole set
# it wants there; one transaction adds what is missing, rewrites what
# changed and deletes the rest.  An edge kept unchanged is not written, so
# its revision does not move.  Reconciliations of one type queue on an
# advisory lock; other types run in parallel.

# Edge ids listed in one change record at most; above, ``ids`` is left out
# and ``count`` stays exact.
RECONCILE_MAX_LISTED = 10_000


class _DryRun(Exception):  # noqa: N818 — a control signal: roll back after counting
    pass


@dataclass(frozen=True, slots=True)
class ReconcileCounts:
    """What a reconciliation did (or, in a dry run, would do)."""

    added: int
    changed: int
    kept: int
    removed: int
    skipped: list[tuple[str, str]]   # pairs whose end node does not exist, as sent
    skipped_count: int


async def _type_lock(conn: asyncpg.Connection, tenant: str, edge_type: str) -> None:
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0));",
        f"graphos.reconcile:{tenant}:{edge_type}",
    )


def _listed(pairs: Sequence[tuple[str, str]]) -> list[list[str]] | None:
    return [list(p) for p in pairs] if len(pairs) <= RECONCILE_MAX_LISTED else None


async def reconcile_edges(  # noqa: PLR0913, PLR0915 — a scope has this many parts
    pool: asyncpg.Pool,
    *,
    tenant: str,
    edge_type: str,
    src_label: str,
    dst_label: str,
    rows: Sequence[Mapping[str, Any]],
    scope: str,
    anchor: str | None = None,
    props_mode: str = "merge",
    dry_run: bool = False,
    timeout_s: float | None = None,
    check_added: Callable[[Sequence[tuple[str, str]]], None] | None = None,
) -> ReconcileCounts:
    """Make the consumer's ``edge_type`` edges of a scope exactly ``rows``.

    The edges a resolve rule derived (marked ``graphos_derived``) are the
    rules': one in ``rows`` is taken over (kept, its mark cleared), the others
    are left as they are (ADR-0001).

    Args:
        rows: ``{"src_id", "dst_id", "props"}``; ``props`` absent or None
            leaves a kept edge's properties as they are.
        scope: ``type`` (every edge of the type from ``src_label`` to
            ``dst_label``, child labels included), ``out`` (the edges from
            the node ``anchor`` of ``src_label`` to ``dst_label``) or ``in``
            (the edges from ``src_label`` into the node ``anchor`` of
            ``dst_label``).
        anchor: the anchor node's graphid, for ``out`` and ``in``.
        props_mode: ``merge`` (into the kept edge's properties) or
            ``replace`` (the given properties become all of them; graphos's
            own ``graphos_*`` keys stay).
        check_added: called, before anything is committed, with the added
            edges that came without properties (they are written as ``{}``);
            raising undoes the whole reconcile.

    Returns:
        The counts; with ``dry_run`` nothing is written.
    """
    records = [
        (
            str(r["src_id"]), str(r["dst_id"]),
            None if r.get("props") is None else json.dumps(r["props"]),
        )
        for r in rows
    ]
    if scope == "type":
        in_scope = (
            f'EXISTS (SELECT 1 FROM "{tenant}"."{src_label}" s WHERE s.id = e.start_id) '
            f'AND EXISTS (SELECT 1 FROM "{tenant}"."{dst_label}" d WHERE d.id = e.end_id)'
        )
    elif scope == "out":
        in_scope = (
            "e.start_id = $1::text::graphid "
            f'AND EXISTS (SELECT 1 FROM "{tenant}"."{dst_label}" d WHERE d.id = e.end_id)'
        )
    elif scope == "in":
        in_scope = (
            "e.end_id = $1::text::graphid "
            f'AND EXISTS (SELECT 1 FROM "{tenant}"."{src_label}" s WHERE s.id = e.start_id)'
        )
    else:
        raise ValueError(f"unknown reconcile scope {scope!r}")
    scope_args: tuple[Any, ...] = () if scope == "type" else (anchor,)
    table = f'"{tenant}"."{edge_type}"'
    detail = {"reconcile": "type" if scope == "type" else "node"}
    # The properties a kept edge ends with: never the rules' mark.
    if props_mode == "replace":
        new_props = (
            f"(CASE WHEN w.props IS NULL THEN e.properties::text::jsonb "
            f"ELSE w.props || COALESCE((SELECT jsonb_object_agg(k, v) "
            f"FROM jsonb_each(e.properties::text::jsonb) AS old(k, v) "
            f"WHERE k LIKE 'graphos\\_%'), '{{}}'::jsonb) END) - '{DERIVED}'"
        )
    else:
        new_props = f"(e.properties::text::jsonb || COALESCE(w.props, '{{}}'::jsonb)) - '{DERIVED}'"

    counts: ReconcileCounts | None = None
    with _db_errors(
        "edge reconcile failed", query=f"reconcile {edge_type}", graph=tenant,
        edge_type=edge_type, scope=scope,
    ):
        try:
            async with pool.acquire() as conn, conn.transaction():
                await _set_timeout(conn, timeout_s)
                await _type_lock(conn, tenant, edge_type)
                await conn.execute("SET LOCAL enable_hashjoin = off;")
                await conn.execute("SET LOCAL work_mem = '256MB';")
                await conn.execute(
                    "CREATE TEMP TABLE _stg_r (src_ext text, dst_ext text, props jsonb, "
                    "n bigserial) ON COMMIT DROP;"
                )
                await conn.copy_records_to_table(
                    "_stg_r", records=records, columns=("src_ext", "dst_ext", "props"),
                )
                # The wanted edges as graphids; the last row of a repeated pair wins.
                await conn.execute(
                    "CREATE TEMP TABLE _want ON COMMIT DROP AS "
                    "SELECT src.id AS start_id, dst.id AS end_id, stg.src_ext, stg.dst_ext, "
                    "       stg.props "
                    "FROM (SELECT DISTINCT ON (src_ext, dst_ext) src_ext, dst_ext, props "
                    "        FROM _stg_r ORDER BY src_ext, dst_ext, n DESC) stg "
                    f'JOIN "{tenant}"."{src_label}" src '
                    f"  ON src.properties->'\"id\"' = to_jsonb(stg.src_ext)::text::agtype "
                    f'JOIN "{tenant}"."{dst_label}" dst '
                    f"  ON dst.properties->'\"id\"' = to_jsonb(stg.dst_ext)::text::agtype;"
                )
                await conn.execute("CREATE INDEX ON _want (start_id, end_id);")
                await conn.execute("ANALYZE _want;")
                found = {
                    (r["src_ext"], r["dst_ext"])
                    for r in await conn.fetch("SELECT DISTINCT src_ext, dst_ext FROM _want;")
                }
                wanted = await conn.fetchval("SELECT count(*) FROM _want;")
                sent = list(dict.fromkeys((s, d) for s, d, _ in records))
                skipped = [p for p in sent if p not in found]

                # Remove what is in the scope and not wanted, the rules' edges aside.
                removed_rows = await conn.fetch(
                    f"WITH gone AS ("
                    f"  DELETE FROM ONLY {table} e WHERE {in_scope} AND NOT ({_MARKED}) "
                    f"  AND NOT EXISTS (SELECT 1 FROM _want w "
                    f"                  WHERE w.start_id = e.start_id AND w.end_id = e.end_id) "
                    f"  RETURNING e.start_id, e.end_id) "
                    f"SELECT s.properties::text::jsonb ->> 'id' AS src_ext, "
                    f"       d.properties::text::jsonb ->> 'id' AS dst_ext "
                    f'FROM gone JOIN "{tenant}"."_ag_label_vertex" s ON s.id = gone.start_id '
                    f'JOIN "{tenant}"."_ag_label_vertex" d ON d.id = gone.end_id;',
                    *scope_args,
                )
                removed = [(r["src_ext"], r["dst_ext"]) for r in removed_rows]

                # Rewrite the kept edges whose properties change (a derived one
                # named here changes: it loses the mark).
                changed_rows = await conn.fetch(
                    f"UPDATE {table} e SET properties = ({new_props})::text::agtype, {_BUMP} "
                    f"FROM _want w "
                    f"WHERE e.start_id = w.start_id AND e.end_id = w.end_id "
                    f"AND ({new_props}) IS DISTINCT FROM e.properties::text::jsonb "
                    f"RETURNING w.src_ext, w.dst_ext;"
                )
                changed = [(r["src_ext"], r["dst_ext"]) for r in changed_rows]

                # Add the wanted edges that are not stored.
                added_rows = await conn.fetch(
                    f"WITH ins AS ("
                    f"  INSERT INTO {table} (start_id, end_id, properties) "
                    f"  SELECT w.start_id, w.end_id, "
                    f"         COALESCE(w.props, '{{}}'::jsonb)::text::agtype "
                    f"  FROM _want w "
                    f"  WHERE NOT EXISTS (SELECT 1 FROM {table} e "
                    f"                    WHERE e.start_id = w.start_id AND e.end_id = w.end_id) "
                    f"  ON CONFLICT (start_id, end_id) DO NOTHING "
                    f"  RETURNING start_id, end_id) "
                    f"SELECT w.src_ext, w.dst_ext, w.props IS NULL AS bare FROM ins "
                    f"JOIN _want w ON w.start_id = ins.start_id AND w.end_id = ins.end_id;"
                )
                added = [(r["src_ext"], r["dst_ext"]) for r in added_rows]
                bare = [(r["src_ext"], r["dst_ext"]) for r in added_rows if r["bare"]]
                if check_added is not None and bare:
                    check_added(bare)

                written = [*added, *changed]
                if written:
                    await changes.record(
                        conn, tenant=tenant, op="upsert", kind="edge", label=edge_type,
                        src_label=src_label, dst_label=dst_label,
                        ids=_listed(written), count=len(written), detail=detail,
                    )
                if removed:
                    await changes.record(
                        conn, tenant=tenant, op="delete", kind="edge", label=edge_type,
                        src_label=src_label, dst_label=dst_label,
                        ids=_listed(removed), count=len(removed), detail=detail,
                    )
                # Counted per stored edge: an id in two child labels is two edges.
                counts = ReconcileCounts(
                    added=len(added), changed=len(changed),
                    kept=wanted - len(added) - len(changed), removed=len(removed),
                    skipped=skipped[:100], skipped_count=len(skipped),
                )
                if dry_run:
                    raise _DryRun
        except _DryRun:
            pass
    assert counts is not None
    return counts


async def reconcile_resolved_edges(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    compiled: CompiledEdgeType,
    adopt_unmarked: bool,
    dry_run: bool,
    timeout_s: float,
) -> tuple[int, int, int]:
    """``edges:resolve`` by difference for one edge type (ADR-0001).

    The rules' edges are computed into a staging table; marked edges
    (``graphos_derived``) no rule derives any more are deleted, and the new
    ones inserted, marked.  ``adopt_unmarked`` also takes the unmarked edges
    of the type between the rules' label pairs as the rules' own: those the
    rules derive are marked, the others deleted.  An edge a consumer wrote
    is never removed otherwise.

    Returns:
        ``(added, removed, marked)``; with ``dry_run`` nothing is written.
    """
    # No rule (any more), or rules no label matches: nothing is derived, so
    # every marked edge of the type goes.
    candidates_sql = compiled.candidates_sql or (
        "SELECT NULL::graphid AS start_id, NULL::graphid AS end_id WHERE false"
    )
    params = compiled.params if compiled.candidates_sql else ()
    table = f'"{tenant}"."{compiled.edge_type}"'
    marked_sql = _MARKED
    pairs_sql = " OR ".join(
        f'(EXISTS (SELECT 1 FROM "{tenant}"."{sl}" s WHERE s.id = e.start_id) '
        f'AND EXISTS (SELECT 1 FROM "{tenant}"."{dl}" d WHERE d.id = e.end_id))'
        for sl, dl in compiled.label_pairs
    ) or "false"
    removable = f"({marked_sql})" + (f" OR (NOT {marked_sql} AND ({pairs_sql}))"
                                      if adopt_unmarked else "")
    result: tuple[int, int, int] | None = None
    with _db_errors(
        "resolve reconcile failed", query=f"resolve reconcile :{compiled.edge_type}",
        edge_type=compiled.edge_type, arms=compiled.arm_count,
    ):
        try:
            async with pool.acquire() as conn, conn.transaction():
                await allow_long_statements(conn, timeout_s)
                await _type_lock(conn, tenant, compiled.edge_type)
                await conn.execute(
                    f"CREATE TEMP TABLE _derived ON COMMIT DROP AS {candidates_sql};",
                    *params, timeout=timeout_s,
                )
                await conn.execute("CREATE INDEX ON _derived (start_id, end_id);")
                await conn.execute("ANALYZE _derived;")
                removed = await conn.fetch(
                    f"WITH gone AS ("
                    f"  DELETE FROM ONLY {table} e WHERE ({removable}) "
                    f"  AND NOT EXISTS (SELECT 1 FROM _derived w "
                    f"                  WHERE w.start_id = e.start_id AND w.end_id = e.end_id) "
                    f"  RETURNING e.start_id, e.end_id) "
                    f"SELECT s.properties::text::jsonb ->> 'id' AS src_ext, "
                    f"       d.properties::text::jsonb ->> 'id' AS dst_ext "
                    f'FROM gone JOIN "{tenant}"."_ag_label_vertex" s ON s.id = gone.start_id '
                    f'JOIN "{tenant}"."_ag_label_vertex" d ON d.id = gone.end_id;',
                    timeout=timeout_s,
                )
                marked = 0
                if adopt_unmarked:
                    status = await conn.execute(
                        f"UPDATE {table} e SET properties = "
                        f"(e.properties::text::jsonb || '{{\"graphos_derived\": true}}')"
                        f"::text::agtype, {_BUMP} "
                        f"FROM _derived w WHERE e.start_id = w.start_id "
                        f"AND e.end_id = w.end_id AND NOT {marked_sql};",
                        timeout=timeout_s,
                    )
                    marked = _rows_written(status, default=0)
                added = await conn.fetch(
                    f"INSERT INTO {table} (start_id, end_id, properties) "
                    f"SELECT start_id, end_id, '{{\"graphos_derived\": true}}'::agtype "
                    f"FROM _derived ON CONFLICT (start_id, end_id) DO NOTHING RETURNING 1;",
                    timeout=timeout_s,
                )
                detail = {"reconcile": "resolve"}
                if added:
                    await changes.record(
                        conn, tenant=tenant, op="resolve", kind="edge",
                        label=compiled.edge_type, count=len(added), detail=detail,
                    )
                if marked:
                    await changes.record(
                        conn, tenant=tenant, op="upsert", kind="edge",
                        label=compiled.edge_type, count=marked,
                        detail={**detail, "marked": True},
                    )
                if removed:
                    pairs = [(r["src_ext"], r["dst_ext"]) for r in removed]
                    await changes.record(
                        conn, tenant=tenant, op="delete", kind="edge",
                        label=compiled.edge_type, ids=_listed(pairs), count=len(pairs),
                        detail=detail,
                    )
                result = (len(added), len(removed), marked)
                if dry_run:
                    raise _DryRun
        except _DryRun:
            pass
    assert result is not None
    return result


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
            # The labels written to are recorded as carrying community_id.
            await conn.execute(
                f"WITH updated AS ("
                f'UPDATE "{tenant}"."_ag_label_vertex" t '
                f"SET properties = t.properties || "
                f"('{{\"community_id\": ' || s.community_id || '}}')::agtype, "
                f"{lookup.REV} = t.{lookup.REV} + 1 "
                f"FROM _stg_assignments s "
                f"WHERE t.id = (s.gid::text)::graphid "
                f"RETURNING t.tableoid) "
                f"INSERT INTO graphos_meta.node_properties (tenant, label, property) "
                f"SELECT DISTINCT $1, c.relname, 'community_id' "
                f"FROM updated JOIN pg_class c ON c.oid = updated.tableoid "
                f"ON CONFLICT DO NOTHING;",
                tenant,
                timeout=600.0,
            )
            # One summary: every node in ``assignments`` got a community_id.
            await changes.record(
                conn, tenant=tenant, op="communities", kind="node", count=len(assignments),
            )


# Nodes written per statement by set_node_metrics: each node rewrites all its
# indexes (properties, id, name, search), so a statement over a whole large
# tenant would outlast the statement timeout.
METRICS_BATCH = 25_000


async def set_node_metrics(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    patches: Sequence[tuple[int, str]],
    clears: Sequence[tuple[int, list[str]]],
    names: Sequence[str],
    batch: int | None = None,
) -> int:
    """Write graph-metric values (``graphos_*``) onto nodes, in one transaction.

    Args:
        patches: ``(graphid, json object)``: the values that changed, merged
            into the node's properties.
        clears: ``(graphid, [names])``: values to remove (the node no longer
            takes part, e.g. it lost its last edge).
        names: every property the run computes, recorded in the property
            catalogue for the labels written.
        batch: nodes per statement (default ``METRICS_BATCH``); the transaction
            stays one, so the run is all or nothing and its change record
            commits with it.

    Only the given nodes are touched, so only they get a new revision. One
    change record summarises the run.

    Returns:
        Nodes written (patched or cleared).
    """
    if not patches and not clears:
        return 0
    batch = batch or METRICS_BATCH
    with _db_errors(
        "metrics write-back failed", query="UPDATE _ag_label_vertex metrics", graph=tenant,
    ):
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL synchronous_commit = off;")
            if patches:
                await conn.execute(
                    "CREATE TEMP TABLE _stg_metrics (n int PRIMARY KEY, gid bigint, patch text) "
                    "ON COMMIT DROP;"
                )
                await conn.copy_records_to_table(
                    "_stg_metrics",
                    records=((n, gid, patch) for n, (gid, patch) in enumerate(patches)),
                    columns=("n", "gid", "patch"),
                )
                for first in range(0, len(patches), batch):
                    await allow_long_statements(conn, 600.0)
                    # The labels written to are recorded as carrying the metrics.
                    await conn.execute(
                        f"WITH updated AS ("
                        f'UPDATE "{tenant}"."_ag_label_vertex" t '
                        f"SET properties = t.properties || (s.patch)::agtype, "
                        f"{lookup.REV} = t.{lookup.REV} + 1 "
                        f"FROM _stg_metrics s "
                        f"WHERE s.n >= $3 AND s.n < $4 AND t.id = (s.gid::text)::graphid "
                        f"RETURNING t.tableoid) "
                        f"INSERT INTO graphos_meta.node_properties (tenant, label, property) "
                        f"SELECT DISTINCT $1, c.relname, p.name "
                        f"FROM updated JOIN pg_class c ON c.oid = updated.tableoid "
                        f"CROSS JOIN unnest($2::text[]) AS p(name) "
                        f"ON CONFLICT DO NOTHING;",
                        tenant, list(names), first, first + batch,
                        timeout=600.0,
                    )
            if clears:
                await conn.execute(
                    "CREATE TEMP TABLE _stg_metric_clears "
                    "(n int PRIMARY KEY, gid bigint, names text[]) ON COMMIT DROP;"
                )
                await conn.copy_records_to_table(
                    "_stg_metric_clears",
                    records=((n, gid, keys) for n, (gid, keys) in enumerate(clears)),
                    columns=("n", "gid", "names"),
                )
                for first in range(0, len(clears), batch):
                    await allow_long_statements(conn, 600.0)
                    await conn.execute(
                        f'UPDATE "{tenant}"."_ag_label_vertex" t '
                        f"SET properties = "
                        f"((((t.properties)::text)::jsonb - s.names)::text)::agtype, "
                        f"{lookup.REV} = t.{lookup.REV} + 1 "
                        f"FROM _stg_metric_clears s "
                        f"WHERE s.n >= $1 AND s.n < $2 AND t.id = (s.gid::text)::graphid;",
                        first, first + batch,
                        timeout=600.0,
                    )
            written = len(patches) + len(clears)
            # One summary: the nodes whose metrics changed.
            await changes.record(conn, tenant=tenant, op="metrics", kind="node", count=written)
    return written


# Vertices per edge-community UPDATE (see :func:`set_edge_end_communities`).
EDGE_COMMUNITY_CHUNK = 50_000


async def set_edge_end_communities(
    pool: asyncpg.Pool,
    *,
    tenant: str,
    assignments: Iterable[tuple[int, str]],
) -> int:
    """Backfill ``end_community_id`` and ``start_community_id`` (the
    communities of each edge's two ends) onto every edge of *tenant*.

    The ``(graphid, community_id)`` pairs are COPYed once into a staging
    table, streamed from ``assignments`` (a generator is fine), then each
    edge label table gets UPDATEs joined to it, per end and per chunk of
    ``EDGE_COMMUNITY_CHUNK`` vertices: a first fill of a column on millions of
    edges (an existing tenant after an upgrade) is then many short statements,
    each well within its time limit, rather than one per label.  ``IS
    DISTINCT FROM`` skips rows whose value is unchanged, so later runs write
    only what moved.  Unlike the other writes, each UPDATE commits on its own,
    so a failure keeps what was written.

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
                await conn.execute("CREATE INDEX ON _stg_edge_communities (gid);")
                # So each per-label plan sees the staging cardinality.
                await conn.execute("ANALYZE _stg_edge_communities;")
                # Chunk boundaries: every EDGE_COMMUNITY_CHUNK-th vertex id.
                starts = [int(r["gid"]) for r in await conn.fetch(
                    "SELECT gid FROM (SELECT gid, row_number() OVER (ORDER BY gid) AS rn "
                    "                 FROM _stg_edge_communities) t "
                    "WHERE (rn - 1) % $1 = 0 ORDER BY gid;",
                    EDGE_COMMUNITY_CHUNK,
                )]
                chunks = list(zip(starts, [*starts[1:], None], strict=True))
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
                    for column, end in (("end_community_id", "end_id"),
                                        ("start_community_id", "start_id")):
                        for low, high in chunks:
                            updated = await conn.execute(
                                f'UPDATE "{tenant}"."{label}" e '
                                f"SET {column} = s.cid "
                                f"FROM _stg_edge_communities s "
                                f"WHERE e.{end} = (s.gid::text)::graphid "
                                f"  AND s.gid >= $1 AND ($2::bigint IS NULL OR s.gid < $2) "
                                f"  AND e.{column} IS DISTINCT FROM s.cid;",
                                low, high, timeout=300.0,
                            )
                            total += _rows_written(updated, default=0)
                    log.info(
                        "louvain.edge_writeback_label_done",
                        extra={"tenant": tenant, "label": label, "total_updated_so_far": total},
                    )
            finally:
                await conn.execute("DROP TABLE IF EXISTS _stg_edge_communities;")

    return total


async def store_community_map(
    pool: asyncpg.Pool, tenant: str, data: CommunityMapData, *, modularity: float,
) -> None:
    """Replaces the tenant's stored community map (see :mod:`graphos.community_map`)
    with `data`, in one transaction."""
    async with pool.acquire() as conn, conn.transaction():
        names = {
            r["id"]: r["name"]
            for r in await conn.fetch(
                "SELECT l.id, l.name FROM ag_catalog.ag_label l "
                "JOIN ag_catalog.ag_graph g ON g.graphid = l.graph WHERE g.name = $1",
                tenant,
            )
        }
        await conn.execute("DELETE FROM graphos_meta.community_map WHERE tenant = $1", tenant)
        await conn.execute("DELETE FROM graphos_meta.community_links WHERE tenant = $1", tenant)
        await conn.copy_records_to_table(
            "community_map", schema_name="graphos_meta",
            columns=[
                "tenant", "community_id", "size", "labels", "hub_gid", "hub_degree",
                "internal_edges",
            ],
            records=[
                (
                    tenant, cid, s.size,
                    json.dumps({names.get(lid, str(lid)): n for lid, n in s.labels.most_common()}),
                    s.hub_gid, s.hub_degree, s.internal_edges,
                )
                for cid, s in data.communities.items()
            ],
        )
        await conn.copy_records_to_table(
            "community_links", schema_name="graphos_meta",
            columns=["tenant", "src", "dst", "type", "count"],
            records=[(tenant, cs, cd, rel, n) for (cs, cd, rel), n in data.links.items()],
        )
        await conn.execute(
            """
            INSERT INTO graphos_meta.community_map_runs
              (tenant, computed_at, communities, modularity)
            VALUES ($1, now(), $2, $3)
            ON CONFLICT (tenant) DO UPDATE
              SET computed_at = now(), communities = EXCLUDED.communities,
                  modularity = EXCLUDED.modularity
            """,
            tenant, len(data.communities), modularity,
        )

"""Find one node or one edge by the identity consumers know it by.

A node is named by its ``id`` property.  That id is unique within a label
(``<Label>_id_unique``) but not across labels, so the same id can name a
node in two labels; ``label`` picks one.  An edge is named by its type and
the ids of its two ends (one edge per type and pair of vertices, the
``(start_id, end_id)`` unique index), and the end labels pick one when an
end id exists in several labels.

When an identity matches more than one row and the caller did not narrow
it, :class:`AmbiguousMatchError` lists the candidates instead of picking
one: reads and writes must never land on an arbitrary row.

Both lookups take a connection, so a write can run them inside its own
transaction with ``for_update=True``.  Names (tenant, labels, edge type)
are interpolated: callers pass names already checked against the tenant's
registered spec.  Ids go in as bind parameters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import asyncpg

# Matches a vertex by its id, served by each label's <Label>_id_unique index.
ID_MATCH = "properties->'\"id\"' = $1::text::agtype"


class AmbiguousMatchError(Exception):
    """An identity matched more than one node or edge."""

    def __init__(self, candidates: list[str]) -> None:
        super().__init__(", ".join(candidates))
        self.candidates = candidates


@dataclass(frozen=True, slots=True)
class NodeRow:
    label: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EdgeRow:
    src_label: str
    dst_label: str
    start_gid: str   # graphids as text, for the follow-up write
    end_gid: str
    properties: dict[str, Any] = field(default_factory=dict)


def _vertex_table(tenant: str, label: str | None) -> str:
    # The parent table reaches every label through inheritance, each child
    # served by its own id index.
    return f'"{tenant}"."{label or "_ag_label_vertex"}"'


async def find_node(
    conn: asyncpg.Connection,
    *,
    tenant: str,
    external_id: str,
    label: str | None = None,
    for_update: bool = False,
) -> NodeRow | None:
    """The node with this id (in ``label``, when given), or ``None``.

    Raises:
        AmbiguousMatchError: the id exists in more than one label and no
            ``label`` was given; ``candidates`` are the labels.
    """
    rows = await conn.fetch(
        f"""
        SELECT c.relname AS label, v.properties::text::jsonb AS props
        FROM {_vertex_table(tenant, label)} v
        JOIN pg_class c ON c.oid = v.tableoid
        WHERE v.{ID_MATCH}
        {"FOR UPDATE OF v" if for_update else ""};
        """,
        json.dumps(external_id),
    )
    if len(rows) > 1:
        raise AmbiguousMatchError(sorted(str(r["label"]) for r in rows))
    if not rows:
        return None
    return NodeRow(label=str(rows[0]["label"]), properties=json.loads(rows[0]["props"]))


async def find_edge(  # noqa: PLR0913 — the identity has this many parts
    conn: asyncpg.Connection,
    *,
    tenant: str,
    edge_type: str,
    src_id: str,
    dst_id: str,
    src_label: str | None = None,
    dst_label: str | None = None,
    for_update: bool = False,
) -> EdgeRow | None:
    """The ``edge_type`` edge from ``src_id`` to ``dst_id``, or ``None``.

    Raises:
        AmbiguousMatchError: more than one such edge exists because an end
            id exists in several labels; ``candidates`` are
            ``"<src label>-><dst label>"`` pairs.
    """
    rows = await conn.fetch(
        f"""
        WITH s AS (
            SELECT id, tableoid FROM {_vertex_table(tenant, src_label)}
            WHERE {ID_MATCH}
        ), d AS (
            SELECT id, tableoid FROM {_vertex_table(tenant, dst_label)}
            WHERE {ID_MATCH.replace("$1", "$2")}
        )
        SELECT sc.relname AS src_label, dc.relname AS dst_label,
               e.start_id::text AS start_gid, e.end_id::text AS end_gid,
               e.properties::text::jsonb AS props
        FROM s CROSS JOIN d
        JOIN "{tenant}"."{edge_type}" e ON e.start_id = s.id AND e.end_id = d.id
        JOIN pg_class sc ON sc.oid = s.tableoid
        JOIN pg_class dc ON dc.oid = d.tableoid
        {"FOR UPDATE OF e" if for_update else ""};
        """,
        json.dumps(src_id),
        json.dumps(dst_id),
    )
    if len(rows) > 1:
        raise AmbiguousMatchError(sorted(f"{r['src_label']}->{r['dst_label']}" for r in rows))
    if not rows:
        return None
    r = rows[0]
    return EdgeRow(
        src_label=str(r["src_label"]),
        dst_label=str(r["dst_label"]),
        start_gid=str(r["start_gid"]),
        end_gid=str(r["end_gid"]),
        properties=json.loads(r["props"]),
    )

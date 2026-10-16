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

Each row found carries its version: its graphid and its revision
(``graphos_rev``, bumped by every write in :mod:`graphos.writes`).  Its
:attr:`etag` is ``"<graphid>.<rev>"``; the graphid part keeps a node or edge
deleted and created again from matching an ETag of the old one.
:class:`IfMatch` is a caller's ``If-Match`` precondition against it.

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

# Matches vertices by any of several ids (``$1``: JSON-encoded ids, as
# ``text[]``), served by the same index.
IDS_MATCH = "properties->'\"id\"' = ANY($1::text[]::agtype[])"

# The revision column on every label table (added to AGE's parent tables at
# bootstrap, so every label table inherits it).
REV = "graphos_rev"


class AmbiguousMatchError(Exception):
    """An identity matched more than one node or edge."""

    def __init__(self, candidates: list[str]) -> None:
        super().__init__(", ".join(candidates))
        self.candidates = candidates


class PreconditionFailedError(Exception):
    """An ``If-Match`` precondition did not hold.

    ``current`` is the ETag the row has now, or ``None`` if it does not exist.
    """

    def __init__(self, current: str | None) -> None:
        super().__init__(current or "no such row")
        self.current = current


def etag(gid: str, rev: int) -> str:
    return f'"{gid}.{rev}"'


@dataclass(frozen=True, slots=True)
class IfMatch:
    """An ``If-Match`` precondition: the ETags a caller accepts.

    ``etags`` is ``None`` for ``*`` (any current version, as long as the row
    exists).  Comparison is the strong one ``If-Match`` requires, so a weak
    ``W/"..."`` tag never matches.
    """

    etags: frozenset[str] | None

    @classmethod
    def parse(cls, header: str) -> IfMatch:
        value = header.strip()
        if value == "*":
            return cls(None)
        return cls(frozenset(t.strip() for t in value.split(",") if t.strip()))

    def check(self, current: str | None) -> None:
        """Raise :class:`PreconditionFailedError` unless ``current`` matches."""
        if current is None or (self.etags is not None and current not in self.etags):
            raise PreconditionFailedError(current)


@dataclass(frozen=True, slots=True)
class NodeRow:
    label: str
    gid: str   # graphid as text
    rev: int
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def etag(self) -> str:
        return etag(self.gid, self.rev)


@dataclass(frozen=True, slots=True)
class EdgeRow:
    src_label: str
    dst_label: str
    gid: str   # graphids as text
    start_gid: str
    end_gid: str
    rev: int
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def etag(self) -> str:
        return etag(self.gid, self.rev)


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
        SELECT c.relname AS label, v.id::text AS gid, v.{REV} AS rev,
               v.properties::text::jsonb AS props
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
    r = rows[0]
    return NodeRow(
        label=str(r["label"]), gid=str(r["gid"]), rev=int(r["rev"]),
        properties=json.loads(r["props"]),
    )


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
               e.id::text AS gid, e.start_id::text AS start_gid,
               e.end_id::text AS end_gid, e.{REV} AS rev,
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
        gid=str(r["gid"]),
        start_gid=str(r["start_gid"]),
        end_gid=str(r["end_gid"]),
        rev=int(r["rev"]),
        properties=json.loads(r["props"]),
    )

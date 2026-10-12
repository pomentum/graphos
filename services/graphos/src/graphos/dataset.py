"""A tenant's data as one file: the dataset export (``graphos.snapshot``).

The file is the NDJSON that ``POST …/graph/snapshot-import`` reads, framed by
two lines that make it self-describing:

    {"_kind": "header", "format": "graphos.snapshot", "version": 1, "graphos": …,
     "tenant": …, "exported_at": …, "computed": true,
     "counts": {"nodes": …, "edges": …}, "schema": {…the schema document…}}
    {"_kind": "node", "id": …, "label": …, "properties": {…}}          (every node)
    {"_kind": "edge", "src_id": …, "src_label": …, "dst_id": …, "dst_label": …,
     "type": …, "properties": {…}}                                     (every edge)
    {"_kind": "end", "counts": {"nodes": …, "edges": …}}

Nodes come first, label by label in id order, each with its own (leaf)
label: a label's table is read ``ONLY``, so a child label's nodes are not
read again through its parent.  Edges follow, type by type, in no set order
within a type (ordering millions of them would cost a sort per export); an
import therefore never meets an edge before its ends.  The ``end`` line
carries what was written: a file without it was cut off.

Consistent: everything is read in one ``REPEATABLE READ`` transaction, so
the file is the graph as of one moment.  Memory stays flat: each table is
read through a server-side cursor, and the edges' ends are resolved by a
join against a temporary table of every vertex's graphid, external id and
label, built once per export (a page-by-page lookup would scan every vertex
table per page; ``_edge_scan_page_sql`` explains).

Not in the file: AGE graphids (reassigned on import; the external ids are
the key), revisions, the edges' denormalised community columns (an import
restores them from the nodes' ``community_id``), the stored community map,
grants.  With ``computed=False`` the ``graphos_*`` and ``community_id``
properties are left out too.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import asyncpg

from graphos import __version__, schema_document
from graphos.bootstrap import TenantSpec
from graphos.db import allow_long_statements

FORMAT = "graphos.snapshot"
VERSION = 1

# Rows fetched per round trip from a server-side cursor.
_FETCH = 2000
# The temporary table of every vertex's end-of-edge details, per export.
_ENDS = "_graphos_export_ends"
# Long enough to build the ends table over millions of vertices.
_EXPORT_STATEMENT_S = 900.0


def _props_sql(column: str, *, computed: bool) -> str:
    """The property bag as canonical JSON text; without computed values
    (``graphos_*``, ``community_id``) unless ``computed``."""
    doc = f"(({column})::text)::jsonb"
    if computed:
        return f"{doc}::text"
    return (
        f"({doc} - COALESCE((SELECT array_agg(k) FROM jsonb_object_keys({doc}) AS k "
        r"WHERE k LIKE 'graphos\_%' OR k = 'community_id'), '{}'::text[]))::text"
    )


def _line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


async def lines(
    pool: asyncpg.Pool,
    spec: TenantSpec,
    *,
    labels: Sequence[str] | None = None,
    types: Sequence[str] | None = None,
    computed: bool = True,
) -> AsyncIterator[str]:
    """The export, line by line (header, nodes, edges, end).

    ``labels`` / ``types`` narrow it to some vertex labels and edge types;
    an edge is exported only when both its ends are.
    """
    tenant = spec.name
    vertex_labels = sorted(labels if labels is not None else spec.vertex_labels)
    edge_types = sorted(types if types is not None else spec.edge_labels)
    async with pool.acquire() as conn:
        await allow_long_statements(conn, _EXPORT_STATEMENT_S)
        async with conn.transaction(isolation="repeatable_read"):
            # A cursor's first rows are wanted as much as its last: plan the
            # whole scan (hash joins), not a fast start (nested loops).
            await conn.execute("SET LOCAL cursor_tuple_fraction = 1.0;")
            await conn.execute("SET LOCAL jit = off;")
            node_counts = {
                label: await conn.fetchval(f'SELECT count(*) FROM ONLY "{tenant}"."{label}";')
                for label in vertex_labels
            }
            edge_counts = {
                t: await conn.fetchval(f'SELECT count(*) FROM ONLY "{tenant}"."{t}";')
                for t in edge_types
            }
            now = datetime.now(UTC)
            yield _line({
                "_kind": "header",
                "format": FORMAT,
                "version": VERSION,
                "graphos": __version__,
                "tenant": tenant,
                "exported_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "computed": computed,
                # Edges whose end is missing, or outside ``labels``, are left
                # out, so the edge count is an upper bound; ``end`` is exact.
                "counts": {"nodes": sum(node_counts.values()), "edges": sum(edge_counts.values())},
                "schema": {k: v for k, v in schema_document.document(spec, now=now).items()
                           if k != "_export"},
            })

            nodes = 0
            node_props = _props_sql("v.properties", computed=computed)
            for label in vertex_labels:
                async for row in conn.cursor(
                    f"SELECT ((v.properties)::text)::jsonb ->> 'id' AS id, {node_props} AS props "
                    f'FROM ONLY "{tenant}"."{label}" v '
                    f"""ORDER BY (v.properties -> '"id"'::agtype);""",
                    prefetch=_FETCH,
                ):
                    nodes += 1
                    yield (f'{{"_kind":"node","id":{json.dumps(row["id"], ensure_ascii=False)},'
                           f'"label":{json.dumps(label)},"properties":{row["props"]}}}\n')

            edges = 0
            if edge_types:
                await conn.execute(
                    f"CREATE TEMP TABLE {_ENDS} ON COMMIT DROP AS "
                    "SELECT v.id, ((v.properties)::text)::jsonb ->> 'id' AS ext, "
                    "c.relname::text AS label "
                    f'FROM "{tenant}"."_ag_label_vertex" v JOIN pg_class c ON c.oid = v.tableoid '
                    "WHERE c.relname = ANY($1::text[]);",
                    vertex_labels,
                )
                await conn.execute(f"ANALYZE {_ENDS};")
                edge_props = _props_sql("e.properties", computed=computed)
                for edge_type in edge_types:
                    async for row in conn.cursor(
                        "SELECT s.ext AS src_id, s.label AS src_label, d.ext AS dst_id, "
                        f"d.label AS dst_label, {edge_props} AS props "
                        f'FROM ONLY "{tenant}"."{edge_type}" e '
                        f"JOIN {_ENDS} s ON s.id = e.start_id "
                        f"JOIN {_ENDS} d ON d.id = e.end_id;",
                        prefetch=_FETCH,
                    ):
                        edges += 1
                        yield (
                            '{"_kind":"edge",'
                            f'"src_id":{json.dumps(row["src_id"], ensure_ascii=False)},'
                            f'"src_label":{json.dumps(row["src_label"])},'
                            f'"dst_id":{json.dumps(row["dst_id"], ensure_ascii=False)},'
                            f'"dst_label":{json.dumps(row["dst_label"])},'
                            f'"type":{json.dumps(edge_type)},"properties":{row["props"]}}}\n'
                        )
            yield _line({"_kind": "end", "counts": {"nodes": nodes, "edges": edges}})


async def chunks(source: AsyncIterator[str], *, gzip: bool) -> AsyncIterator[bytes]:
    """The lines as response chunks of about 64 KiB, gzip-compressed if asked."""
    packer = zlib.compressobj(6, zlib.DEFLATED, 31) if gzip else None
    buffer: list[str] = []
    size = 0
    async for text in source:
        buffer.append(text)
        size += len(text)
        if size >= 65536:   # noqa: PLR2004 — chunk size
            data = "".join(buffer).encode("utf-8")
            buffer, size = [], 0
            out = packer.compress(data) if packer else data
            if out:
                yield out
    data = "".join(buffer).encode("utf-8")
    if packer:
        yield packer.compress(data) + packer.flush()
    elif data:
        yield data


# ── Reading a file back ─────────────────────────────────────────────────────


def check_header(header: dict[str, Any]) -> None:
    """Refuse a header of another format, or of a newer version."""
    if header.get("format", FORMAT) != FORMAT:
        raise ValueError(f"the file's format is {header.get('format')!r}, not {FORMAT!r}")
    version = header.get("version", VERSION)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("the header's `version` must be a positive integer")
    if version > VERSION:
        raise ValueError(
            f"this file is format version {version}; this server reads up to version "
            f"{VERSION} (it was exported by graphos {header.get('graphos', '?')})"
        )


GZIP_MAGIC = bytes((0x1F, 0x8B))   # what every gzip stream starts with


class BodyTooLarge(Exception):  # noqa: N818 — reads as the condition
    """The (decompressed) body passed the import's size cap."""


class Gunzip:
    """Decompresses a body that starts with the gzip magic bytes and passes
    any other through, counting what it hands on against ``max_bytes``.

    Inflates in steps of at most ``_STEP`` bytes, so a small compressed chunk
    that expands to gigabytes (a zip bomb) stops at the cap instead of being
    inflated in one piece.
    """

    _STEP = 1 << 20

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.size = 0
        self._inflater: Any = None
        self._pending = b""
        self._decided = False

    def _count(self, out: bytes) -> bytes:
        self.size += len(out)
        if self.size > self.max_bytes:
            raise BodyTooLarge(f"the body passed {self.max_bytes} bytes")
        return out

    def feed(self, chunk: bytes) -> list[bytes]:
        if not self._decided:
            self._pending += chunk
            if len(self._pending) < 2:   # noqa: PLR2004 — the magic's length
                return []
            self._decided = True
            if self._pending[:2] == GZIP_MAGIC:
                self._inflater = zlib.decompressobj(31)
            chunk, self._pending = self._pending, b""
        if self._inflater is None:
            return [self._count(chunk)]
        out: list[bytes] = []
        data = chunk
        while data:
            piece = self._inflater.decompress(data, self._STEP)
            out.append(self._count(piece))
            data = self._inflater.unconsumed_tail
        return out

    def finish(self) -> list[bytes]:
        if not self._decided:
            self._decided = True
            return [self._count(self._pending)] if self._pending else []
        if self._inflater is None:
            return []
        return [self._count(self._inflater.flush())]

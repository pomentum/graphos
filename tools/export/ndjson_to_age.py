"""Load a graphos dataset export into a plain Apache AGE graph, without graphos.

    python tools/export/ndjson_to_age.py EXPORT.ndjson[.gz] GRAPH --dsn postgresql://...

EXPORT is the file ``GET /v1/{tenant}/graph/snapshot`` writes (``graphos.snapshot``, version 1).
GRAPH is a new AGE graph: it is created with one label table per node label and edge type, with
AGE's own columns only (no graphos columns, so AGE's variable-length patterns work on it), and
filled with every node and edge of the file. Node ids are the file's external ids
(``properties.id``); AGE assigns its own graph ids. Needs only ``asyncpg`` and a database with
the ``age`` extension, loaded by ``shared_preload_libraries``.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import sys
from collections.abc import Iterator

import asyncpg

FORMAT = "graphos.snapshot"
BATCH = 50_000


def lines(path: str) -> Iterator[dict]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def load(path: str, graph: str, dsn: str) -> dict[str, int]:
    conn = await asyncpg.connect(dsn, server_settings={"search_path": 'ag_catalog, "$user", public'})
    try:
        head = None
        nodes: dict[str, list[tuple[str, str]]] = {}
        edges: dict[str, list[tuple[str, str, str, str, str]]] = {}
        end = None
        for row in lines(path):
            kind = row.get("_kind")
            if kind == "header":
                head = row
            elif kind == "node":
                nodes.setdefault(row["label"], []).append((row["id"], json.dumps(row["properties"])))
            elif kind == "edge":
                edges.setdefault(row["type"], []).append((
                    row["src_label"], row["src_id"], row["dst_label"], row["dst_id"],
                    json.dumps(row["properties"]),
                ))
            elif kind == "end":
                end = row
        if head is None or head.get("format") != FORMAT:
            raise SystemExit(f"{path}: not a {FORMAT} file")
        if end is None:
            raise SystemExit(f"{path}: no end line, the file was cut off")

        async with conn.transaction():
            await conn.execute("SELECT ag_catalog.create_graph($1);", graph)
            for label in nodes:
                await conn.execute("SELECT ag_catalog.create_vlabel($1, $2);", graph, label)
            for edge_type in edges:
                await conn.execute("SELECT ag_catalog.create_elabel($1, $2);", graph, edge_type)
            g = ident(graph)
            await conn.execute(
                "CREATE TEMP TABLE _stg_v (label text, ext text, props text) ON COMMIT DROP;"
                "CREATE TEMP TABLE _stg_e (type text, src_label text, src text, dst_label text,"
                " dst text, props text) ON COMMIT DROP;",
            )
            for label, rows in nodes.items():
                for i in range(0, len(rows), BATCH):
                    await conn.copy_records_to_table(
                        "_stg_v", records=[(label, ext, props) for ext, props in rows[i:i + BATCH]],
                    )
                await conn.execute(
                    f"INSERT INTO {g}.{ident(label)} (properties) "
                    f"SELECT props::agtype FROM _stg_v WHERE label = $1;", label,
                )
            await conn.execute(
                f"CREATE TEMP TABLE _ids ON COMMIT DROP AS "
                f"SELECT c.relname::text AS label, v.properties::text::jsonb ->> 'id' AS ext, v.id "
                f"FROM {g}._ag_label_vertex v JOIN pg_class c ON c.oid = v.tableoid;"
                f"CREATE UNIQUE INDEX ON _ids (label, ext);"
                f"ANALYZE _ids;",
            )
            for edge_type, rows in edges.items():
                for i in range(0, len(rows), BATCH):
                    await conn.copy_records_to_table(
                        "_stg_e", records=[(edge_type, *r) for r in rows[i:i + BATCH]],
                    )
                await conn.execute(
                    f"INSERT INTO {g}.{ident(edge_type)} (start_id, end_id, properties) "
                    f"SELECT s.id, d.id, e.props::agtype FROM _stg_e e "
                    f"JOIN _ids s ON s.label = e.src_label AND s.ext = e.src "
                    f"JOIN _ids d ON d.label = e.dst_label AND d.ext = e.dst "
                    f"WHERE e.type = $1;", edge_type,
                )
            loaded_nodes = await conn.fetchval(f"SELECT count(*) FROM {g}._ag_label_vertex;")
            loaded_edges = await conn.fetchval(f"SELECT count(*) FROM {g}._ag_label_edge;")
        want = end["counts"]
        if (loaded_nodes, loaded_edges) != (want["nodes"], want["edges"]):
            raise SystemExit(
                f"loaded {loaded_nodes} nodes and {loaded_edges} edges, "
                f"the file says {want['nodes']} and {want['edges']}",
            )
        return {"nodes": loaded_nodes, "edges": loaded_edges}
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("export", help="the graphos dataset export (.ndjson or .ndjson.gz)")
    parser.add_argument("graph", help="the new AGE graph to create")
    parser.add_argument("--dsn", required=True, help="postgresql://user:password@host:port/db")
    args = parser.parse_args()
    counts = asyncio.run(load(args.export, args.graph, args.dsn))
    print(f"{args.graph}: {counts['nodes']} nodes, {counts['edges']} edges", file=sys.stderr)


if __name__ == "__main__":
    main()

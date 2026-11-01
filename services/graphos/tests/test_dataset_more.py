"""The dataset file (``graphos.dataset``) without a database: the header check,
the gzip reader with its size cap, the chunking of the response, and the export's
framing over a faked connection (``test_dataset_db.py`` exports for real)."""

from __future__ import annotations

import gzip
import json
import re
import zlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from graphos import __version__, dataset
from graphos.bootstrap import TenantSpec

# ── The header ─────────────────────────────────────────────────────────────


def test_a_header_of_this_format_and_version_is_accepted() -> None:
    dataset.check_header({"format": dataset.FORMAT, "version": dataset.VERSION, "graphos": "1.0"})


def test_a_header_without_format_or_version_reads_as_this_one() -> None:
    dataset.check_header({})
    dataset.check_header({"tenant": "acme", "counts": {"nodes": 1, "edges": 0}})


@pytest.mark.parametrize("fmt", ["graphos.schema", "neo4j.dump", "", None])
def test_a_header_of_another_format_is_refused_naming_both(fmt: object) -> None:
    with pytest.raises(ValueError, match="the file's format is") as refused:
        dataset.check_header({"format": fmt, "version": 1})
    assert repr(dataset.FORMAT) in str(refused.value)


def test_a_newer_version_is_refused_naming_the_exporting_graphos() -> None:
    with pytest.raises(ValueError, match="format version 2") as refused:
        dataset.check_header({"format": dataset.FORMAT, "version": 2, "graphos": "9.9.9"})
    assert "reads up to version 1" in str(refused.value)
    assert "graphos 9.9.9" in str(refused.value)


def test_a_newer_version_without_the_exporters_version_says_so() -> None:
    with pytest.raises(ValueError, match=r"exported by graphos \?"):
        dataset.check_header({"version": 7})


@pytest.mark.parametrize("version", [0, -1, "1", 1.0, 1.5, True, None, [1]])
def test_a_version_that_is_not_a_positive_integer_is_refused(version: object) -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        dataset.check_header({"format": dataset.FORMAT, "version": version})


# ── Gunzip: what the import reads ──────────────────────────────────────────


def _through(reader: dataset.Gunzip, *chunks: bytes) -> bytes:
    out: list[bytes] = []
    for chunk in chunks:
        out.extend(reader.feed(chunk))
    out.extend(reader.finish())
    return b"".join(out)


def test_a_plain_body_passes_through_unchanged() -> None:
    body = b'{"_kind":"node","id":"a","label":"Person"}\n'
    reader = dataset.Gunzip(1 << 20)
    assert _through(reader, body[:5], body[5:]) == body
    assert reader.size == len(body)


def test_a_gzip_body_is_decompressed_in_any_chunking() -> None:
    body = b"".join(b'{"_kind":"node","id":"n%d","label":"Person"}\n' % i for i in range(500))
    packed = gzip.compress(body)
    whole = _through(dataset.Gunzip(1 << 20), packed)
    one_byte_at_a_time = _through(dataset.Gunzip(1 << 20), *(packed[i:i + 1]
                                                               for i in range(len(packed))))
    assert whole == body
    assert one_byte_at_a_time == body


def test_the_gzip_magic_split_over_two_chunks_is_still_recognised() -> None:
    packed = gzip.compress(b"hello\n")
    reader = dataset.Gunzip(1024)
    assert reader.feed(packed[:1]) == []
    assert b"".join(reader.feed(packed[1:]) + reader.finish()) == b"hello\n"


def test_a_one_byte_body_is_handed_on_at_the_end() -> None:
    reader = dataset.Gunzip(1024)
    assert reader.feed(b"x") == []
    assert reader.finish() == [b"x"]


def test_an_empty_body_hands_on_nothing() -> None:
    reader = dataset.Gunzip(1024)
    assert reader.feed(b"") == []
    assert reader.finish() == []
    assert reader.size == 0


def test_a_body_that_only_starts_like_gzip_is_passed_through() -> None:
    body = b"\x1f is not gzip\n"
    assert _through(dataset.Gunzip(1024), body) == body


def test_a_plain_body_past_the_cap_is_refused() -> None:
    reader = dataset.Gunzip(10)
    assert reader.feed(b"0123456789") == [b"0123456789"]
    with pytest.raises(dataset.BodyTooLarge, match="passed 10 bytes"):
        reader.feed(b"!")


def test_a_body_of_exactly_the_cap_is_accepted() -> None:
    assert _through(dataset.Gunzip(6), b"abcdef") == b"abcdef"


def test_the_cap_counts_the_decompressed_size_not_the_compressed_one() -> None:
    body = b"a" * 10_000
    packed = gzip.compress(body)
    assert len(packed) < 1000  # noqa: PLR2004 -- the premise
    with pytest.raises(dataset.BodyTooLarge):
        _through(dataset.Gunzip(5000), packed)


def test_a_zip_bomb_stops_at_the_cap_without_inflating_it_whole() -> None:
    packed = gzip.compress(b"\0" * (64 << 20))   # 64 MiB of zeros: ~64 KiB compressed
    reader = dataset.Gunzip(2 << 20)
    with pytest.raises(dataset.BodyTooLarge):
        reader.feed(packed)
    # It stopped one step past the cap, not after inflating 64 MiB.
    assert reader.size <= (2 << 20) + dataset.Gunzip._STEP  # noqa: SLF001


def test_a_highly_compressed_chunk_is_inflated_completely_in_steps() -> None:
    body = b"\0" * (5 << 20) + b"\nend\n"
    reader = dataset.Gunzip(16 << 20)
    pieces = reader.feed(gzip.compress(body)) + reader.finish()
    assert b"".join(pieces) == body
    assert max(len(p) for p in pieces) <= dataset.Gunzip._STEP  # noqa: SLF001


def test_a_corrupt_gzip_stream_raises_a_zlib_error() -> None:
    reader = dataset.Gunzip(1024)
    with pytest.raises(zlib.error):
        reader.feed(dataset.GZIP_MAGIC + b"\x08\x00garbage-not-deflate" * 4)


def test_a_cut_off_gzip_stream_hands_on_what_it_held() -> None:
    body = b"".join(b'{"_kind":"node","id":"n%d","label":"Person"}\n' % i for i in range(50))
    packed = gzip.compress(body)
    got = _through(dataset.Gunzip(1 << 20), packed[: len(packed) // 2])
    assert body.startswith(got)
    assert len(got) < len(body)


def test_concatenated_gzip_members_are_all_read() -> None:
    first, second = b'{"id":"a","label":"Person"}\n', b'{"id":"b","label":"Person"}\n'
    packed = gzip.compress(first) + gzip.compress(second)
    assert gzip.decompress(packed) == first + second   # what gzip itself reads
    assert _through(dataset.Gunzip(1024), packed) == first + second


# ── chunks: what the export sends ──────────────────────────────────────────


async def _source(lines: list[str]) -> AsyncIterator[str]:
    for line in lines:
        yield line


async def _collect(lines: list[str], *, gzip_it: bool) -> list[bytes]:
    return [c async for c in dataset.chunks(_source(lines), gzip=gzip_it)]


async def test_small_output_is_sent_as_one_chunk() -> None:
    lines = ['{"_kind":"header"}\n', '{"_kind":"end"}\n']
    assert await _collect(lines, gzip_it=False) == ["".join(lines).encode()]


async def test_large_output_is_sent_in_chunks_of_about_64_kib() -> None:
    lines = [f'{{"_kind":"node","id":"n{i:06d}","label":"Person"}}\n' for i in range(5000)]
    chunks = await _collect(lines, gzip_it=False)
    assert len(chunks) > 1
    assert all(len(c) >= 65536 for c in chunks[:-1])  # noqa: PLR2004
    assert all(len(c) < 65536 + 100 for c in chunks)  # noqa: PLR2004
    assert b"".join(chunks) == "".join(lines).encode()


async def test_non_ascii_text_is_sent_as_utf8() -> None:
    lines = ['{"id":"Zoë","name":"Ηλέκτρα"}\n']
    assert b"".join(await _collect(lines, gzip_it=False)).decode("utf-8") == lines[0]


async def test_nothing_to_send_sends_nothing_without_gzip() -> None:
    assert await _collect([], gzip_it=False) == []


async def test_nothing_to_send_with_gzip_is_still_a_valid_gzip_file() -> None:
    assert gzip.decompress(b"".join(await _collect([], gzip_it=True))) == b""


async def test_gzip_output_decompresses_to_the_lines_and_reads_back_through_gunzip() -> None:
    lines = [f'{{"_kind":"node","id":"n{i}","label":"Person"}}\n' for i in range(20_000)]
    packed = b"".join(await _collect(lines, gzip_it=True))
    assert packed[:2] == dataset.GZIP_MAGIC
    assert gzip.decompress(packed) == "".join(lines).encode()
    assert _through(dataset.Gunzip(1 << 30), packed) == "".join(lines).encode()


# ── lines: the export's framing, over a faked connection ───────────────────

_TABLE = re.compile(r'FROM ONLY "(?P<tenant>[^"]+)"\."(?P<table>[^"]+)"')


class _Conn:
    """A connection that answers the export's counts and cursors from dicts."""

    def __init__(self, nodes: dict[str, list[tuple[str, dict[str, Any]]]],
                 edges: dict[str, list[tuple[str, str, str, str, dict[str, Any]]]]) -> None:
        self.nodes, self.edges = nodes, edges
        self.executed: list[str] = []
        self.isolation: str | None = None
        self.ends: set[str] = set()

    async def execute(self, sql: str, *args: object) -> str:
        self.executed.append(sql)
        if "CREATE TEMP TABLE" in sql:   # the ends table: the exported labels' vertices
            self.ends = set(args[0])  # type: ignore[call-overload]
        return "OK"

    async def fetchval(self, sql: str) -> int:
        table = _TABLE.search(sql)["table"]
        return len(self.nodes.get(table, self.edges.get(table, [])))

    def transaction(self, *, isolation: str) -> _Conn:
        self.isolation = isolation
        return self

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def cursor(self, sql: str, *, prefetch: int) -> AsyncIterator[dict[str, Any]]:
        assert prefetch > 0
        table = _TABLE.search(sql)["table"]
        if table in self.nodes:
            for node_id, props in self.nodes[table]:
                yield {"id": node_id, "props": json.dumps(props)}
        else:
            for src, src_label, dst, dst_label, props in self.edges.get(table, []):
                # An edge whose end is outside the export is not joined.
                if src_label in self.ends and dst_label in self.ends:
                    yield {"src_id": src, "src_label": src_label, "dst_id": dst,
                           "dst_label": dst_label, "props": json.dumps(props)}


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self) -> _Conn:
        return self.conn


SPEC = TenantSpec(name="acme", vertex_labels=("Person", "Doc"), edge_labels=("KNOWS", "WROTE"))
NODES = {
    "Person": [("p1", {"id": "p1", "name": "Ada"}), ("p2", {"id": "p2", "name": "Zoë"})],
    "Doc": [("d1", {"id": "d1", "title": "Notes", "community_id": "3"})],
}
EDGES = {
    "KNOWS": [("p1", "Person", "p2", "Person", {"since": 2020})],
    "WROTE": [("p1", "Person", "d1", "Doc", {}), ("p2", "Person", "d1", "Doc", {"year": 2026})],
}


async def _export(spec: TenantSpec = SPEC, **kw: Any) -> tuple[list[dict[str, Any]], _Conn]:
    conn = _Conn(NODES, EDGES)
    text = [line async for line in dataset.lines(_Pool(conn), spec, **kw)]  # type: ignore[arg-type]
    assert all(line.endswith("\n") and line.count("\n") == 1 for line in text)
    return [json.loads(line) for line in text], conn


async def test_an_export_is_a_header_then_nodes_then_edges_then_an_end() -> None:
    rows, conn = await _export()
    assert [r["_kind"] for r in rows] == ["header"] + ["node"] * 3 + ["edge"] * 3 + ["end"]
    assert conn.isolation == "repeatable_read"


async def test_the_header_names_the_format_version_tenant_and_counts() -> None:
    rows, _ = await _export()
    header = rows[0]
    assert (header["format"], header["version"]) == (dataset.FORMAT, dataset.VERSION)
    assert (header["tenant"], header["graphos"], header["computed"]) == ("acme", __version__, True)
    assert header["counts"] == {"nodes": 3, "edges": 3}
    assert header["exported_at"].endswith("Z")
    assert "_export" not in header["schema"]
    assert sorted(header["schema"]["vertex_labels"]) == ["Doc", "Person"]
    dataset.check_header(header)   # an export always reads back


async def test_nodes_come_label_by_label_in_name_order_with_their_label() -> None:
    rows, _ = await _export()
    nodes = [(r["label"], r["id"]) for r in rows if r["_kind"] == "node"]
    assert nodes == [("Doc", "d1"), ("Person", "p1"), ("Person", "p2")]
    assert rows[1]["properties"]["community_id"] == "3"


async def test_edges_carry_both_ends_with_their_labels() -> None:
    rows, _ = await _export()
    edges = [r for r in rows if r["_kind"] == "edge"]
    assert [e["type"] for e in edges] == ["KNOWS", "WROTE", "WROTE"]
    assert edges[0] == {"_kind": "edge", "src_id": "p1", "src_label": "Person", "dst_id": "p2",
                        "dst_label": "Person", "type": "KNOWS", "properties": {"since": 2020}}


async def test_non_ascii_ids_and_values_are_written_as_text() -> None:
    conn = _Conn({"Person": [("Zoë-1", {"name": "Ηλέκτρα"})]}, {})
    spec = TenantSpec(name="acme", vertex_labels=("Person",))
    text = [line async for line in dataset.lines(_Pool(conn), spec)]  # type: ignore[arg-type]
    assert '"id":"Zoë-1"' in text[1]
    assert json.loads(text[1])["properties"] == {"name": "Ηλέκτρα"}


async def test_the_end_line_counts_what_was_written_and_the_header_an_upper_bound() -> None:
    rows, _ = await _export(labels=["Person"])
    kinds = [r["_kind"] for r in rows]
    # WROTE edges end at a Doc, which is not exported: they are left out.
    assert kinds == ["header", "node", "node", "edge", "end"]
    assert rows[0]["counts"] == {"nodes": 2, "edges": 3}
    assert rows[-1]["counts"] == {"nodes": 2, "edges": 1}


async def test_types_narrow_the_edges_exported() -> None:
    rows, _ = await _export(types=["WROTE"])
    assert {r["type"] for r in rows if r["_kind"] == "edge"} == {"WROTE"}
    assert rows[-1]["counts"] == {"nodes": 3, "edges": 2}


async def test_without_edge_types_no_end_table_is_built() -> None:
    rows, conn = await _export(types=[])
    assert rows[-1] == {"_kind": "end", "counts": {"nodes": 3, "edges": 0}}
    assert not any("CREATE TEMP TABLE" in sql for sql in conn.executed)


async def test_an_empty_tenant_exports_a_header_and_an_end() -> None:
    rows, _ = await _export(TenantSpec(name="acme"))
    assert [r["_kind"] for r in rows] == ["header", "end"]
    assert rows[0]["counts"] == rows[-1]["counts"] == {"nodes": 0, "edges": 0}


async def test_an_export_without_computed_values_says_so_in_its_header() -> None:
    rows, _ = await _export(computed=False)
    assert rows[0]["computed"] is False

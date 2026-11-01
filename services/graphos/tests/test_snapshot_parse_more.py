"""``POST /v1/{tenant}/graph/snapshot-import`` and ``GET …/snapshot`` without a
database: what the import reads from a file, refuses, reports and hands to the
writers, in what order.

The tenant registry and the SQL writers are replaced; ``test_dataset_db.py``
imports for real.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from graphos import dataset, writes
from graphos.api import graph, snapshot
from graphos.app import create_app
from graphos.bootstrap import TenantSpec
from graphos.config import Settings
from graphos.cypher import CypherError

IMPORT = "/v1/acme/graph/snapshot-import"
EXPORT = "/v1/acme/graph/snapshot"
NDJSON = {"Content-Type": "application/x-ndjson"}

SPEC = TenantSpec(
    name="acme", vertex_labels=("Person", "Doc"), edge_labels=("KNOWS", "WROTE"),
    property_schema={"vertex": {"Doc": {
        "mode": "strict", "properties": {"title": {"type": "string", "required": True}},
    }}},
)
HEADER = {"_kind": "header", "format": dataset.FORMAT, "version": dataset.VERSION,
          "graphos": "1.0.0", "tenant": "acme", "counts": {"nodes": 2, "edges": 1},
          "schema": {"vertex_labels": ["Person", "Doc"]}}
ADA = {"_kind": "node", "id": "p1", "label": "Person", "properties": {"name": "Ada"}}
BOB = {"_kind": "node", "id": "p2", "label": "Person", "properties": {"name": "Bob"}}
KNOWS = {"_kind": "edge", "src_id": "p1", "src_label": "Person", "dst_id": "p2",
         "dst_label": "Person", "type": "KNOWS", "properties": {"since": 2020}}
END = {"_kind": "end", "counts": {"nodes": 2, "edges": 1}}
# The end line of a file holding ADA alone.
END_ONE = {"_kind": "end", "counts": {"nodes": 1, "edges": 0}}


def _ndjson(*rows: object) -> str:
    return "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows)


class _Pool:
    """``fetchval``: whether the tenant holds nodes; ``fetch``: their communities."""

    def __init__(self) -> None:
        self.holds_nodes = False
        self.communities: list[dict[str, Any]] = []

    async def fetchval(self, _sql: str) -> bool:
        return self.holds_nodes

    async def fetch(self, _sql: str) -> list[dict[str, Any]]:
        return self.communities


class _Written:
    """What reached the writers, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[dict[str, Any]]]] = []
        self.missing: set[str] = set()   # node ids whose edges are skipped
        self.node_error: BaseException | None = None
        self.edge_error: BaseException | None = None
        self.communities: list[tuple[int, str]] = []

    def nodes(self, label: str) -> list[dict[str, Any]]:
        return [r for kind, name, rows in self.calls if kind == "nodes" and name == label
                for r in rows]


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> _Written:
    for module in (graph, snapshot):
        monkeypatch.setattr(module, "registered_tenants", lambda: {"acme": SPEC})
    seen = _Written()

    async def upsert_nodes(_pool: object, *, tenant: str, label: str, rows: list) -> int:
        assert tenant == "acme"
        if seen.node_error is not None:
            raise seen.node_error
        seen.calls.append(("nodes", label, list(rows)))
        return len(rows)

    async def upsert_edges(
        _pool: object, *, tenant: str, src_label: str, dst_label: str, edge_type: str,
        rows: list,
    ) -> tuple[int, int]:
        if seen.edge_error is not None:
            raise seen.edge_error
        seen.calls.append(("edges", f"{src_label}-{edge_type}->{dst_label}", list(rows)))
        skipped = sum(1 for r in rows if {r["src_id"], r["dst_id"]} & seen.missing)
        return len(rows) - skipped, skipped

    async def set_edge_end_communities(_pool: object, *, tenant: str, assignments: Any) -> int:
        seen.communities = list(assignments)
        return 2 * len(seen.communities)

    monkeypatch.setattr(writes, "upsert_nodes", upsert_nodes)
    monkeypatch.setattr(writes, "upsert_edges", upsert_edges)
    monkeypatch.setattr(writes, "set_edge_end_communities", set_edge_end_communities)
    return seen


@pytest.fixture
def pool() -> _Pool:
    return _Pool()


@pytest.fixture
def client(pool: _Pool) -> TestClient:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True,
                              import_max_bytes=64 * 1024))
    app.state.pool = pool
    return TestClient(app, raise_server_exceptions=False)


def _post(client: TestClient, body: str | bytes, **params: str) -> Any:
    return client.post(IMPORT, content=body, headers=NDJSON, params=params)


# ── A whole file ───────────────────────────────────────────────────────────


def test_an_exported_file_is_imported_whole_and_reported_complete(
    client: TestClient, written: _Written,
) -> None:
    resp = _post(client, _ndjson(HEADER, ADA, BOB, KNOWS, END))
    assert resp.status_code == HTTPStatus.OK, resp.text
    answer = resp.json()
    assert (answer["nodes_received"], answer["edges_received"]) == (2, 1)
    assert (answer["nodes_upserted"], answer["edges_upserted"]) == (2, 1)
    assert answer["by_node_label"] == {"Person": 2}
    assert answer["by_edge_type"] == {"KNOWS": 1}
    assert answer["complete"] is True
    assert answer["errors"] == []
    assert answer["mode"] == "merge"
    # The header comes back without its schema.
    assert answer["header"] == {k: v for k, v in HEADER.items() if k not in ("_kind", "schema")}
    assert written.calls == [
        ("nodes", "Person", [{"id": "p1", "props": {"name": "Ada"}},
                             {"id": "p2", "props": {"name": "Bob"}}]),
        ("edges", "Person-KNOWS->Person", [{"src_id": "p1", "dst_id": "p2",
                                            "props": {"since": 2020}}]),
    ]


def test_a_file_without_its_end_line_is_reported_cut_off(
    client: TestClient, written: _Written,
) -> None:
    answer = _post(client, _ndjson(HEADER, ADA, BOB, KNOWS)).json()
    assert answer["complete"] is False
    assert answer["nodes_upserted"] == 2   # what was read is still written  # noqa: PLR2004


def test_a_file_without_a_header_is_neither_complete_nor_cut_off(
    client: TestClient, written: _Written,
) -> None:
    answer = _post(client, _ndjson(ADA, BOB, KNOWS, END)).json()
    assert (answer["header"], answer["complete"]) == (None, None)
    assert answer["nodes_upserted"] == 2  # noqa: PLR2004


def test_lines_without_a_kind_are_nodes(client: TestClient, written: _Written) -> None:
    plain = {k: v for k, v in ADA.items() if k != "_kind"}
    answer = _post(client, _ndjson(plain)).json()
    assert answer["nodes_upserted"] == 1
    assert written.nodes("Person") == [{"id": "p1", "props": {"name": "Ada"}}]


def test_kinds_are_read_in_any_case(client: TestClient, written: _Written) -> None:
    answer = _post(client, _ndjson({**HEADER, "_kind": "HEADER"}, {**ADA, "_kind": "Node"},
                                   {**END_ONE, "_kind": "End"})).json()
    assert (answer["nodes_upserted"], answer["complete"]) == (1, True)


def test_an_empty_body_imports_nothing(client: TestClient, written: _Written) -> None:
    answer = _post(client, "").json()
    assert (answer["nodes_received"], answer["edges_received"], answer["errors"]) == (0, 0, [])
    assert written.calls == []


def test_blank_lines_and_a_missing_final_newline_are_fine(
    client: TestClient, written: _Written,
) -> None:
    body = "\n\n" + json.dumps(ADA) + "\n   \n" + json.dumps(END_ONE)
    answer = _post(client, body).json()
    assert (answer["nodes_upserted"], answer["errors"]) == (1, [])


def test_the_top_level_id_wins_over_one_in_the_properties(
    client: TestClient, written: _Written,
) -> None:
    node = {**ADA, "properties": {"id": "other", "name": "Ada"}}
    _post(client, _ndjson(node))
    assert written.nodes("Person") == [{"id": "p1", "props": {"name": "Ada"}}]


def test_computed_values_in_the_file_are_restored_not_refused(
    client: TestClient, written: _Written, pool: _Pool,
) -> None:
    pool.communities = [{"gid": "281474976710657", "cid": "7"}]
    node = {**ADA, "properties": {"name": "Ada", "community_id": "7", "graphos_degree": 3}}
    answer = _post(client, _ndjson(HEADER, node, END_ONE)).json()
    assert answer["errors"] == []
    assert written.nodes("Person")[0]["props"]["graphos_degree"] == 3  # noqa: PLR2004
    # The edges' copies of their ends' communities are restored from the nodes'.
    assert written.communities == [(281474976710657, "7")]
    assert answer["edge_communities_set"] == 2  # noqa: PLR2004


def test_without_communities_in_the_file_the_edges_are_left_alone(
    client: TestClient, written: _Written,
) -> None:
    answer = _post(client, _ndjson(ADA, BOB, KNOWS)).json()
    assert answer["edge_communities_set"] == 0
    assert written.communities == []


def test_a_gzip_body_is_read_whatever_its_content_type(
    client: TestClient, written: _Written,
) -> None:
    body = gzip.compress(_ndjson(HEADER, ADA, BOB, KNOWS, END).encode())
    resp = client.post(IMPORT, content=body, headers={"Content-Type": "application/octet-stream"})
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert (resp.json()["nodes_upserted"], resp.json()["complete"]) == (2, True)


def test_non_ascii_ids_and_values_survive(client: TestClient, written: _Written) -> None:
    node = {"_kind": "node", "id": "Zoë", "label": "Person", "properties": {"name": "Ηλέκτρα"}}
    _post(client, _ndjson(node).encode("utf-8"))
    assert written.nodes("Person") == [{"id": "Zoë", "props": {"name": "Ηλέκτρα"}}]


def test_edges_whose_ends_are_missing_are_counted_skipped(
    client: TestClient, written: _Written,
) -> None:
    written.missing = {"p2"}
    answer = _post(client, _ndjson(ADA, KNOWS)).json()
    assert (answer["edges_upserted"], answer["edges_skipped"]) == (0, 1)


# ── The header ─────────────────────────────────────────────────────────────


def test_a_header_of_another_format_is_refused_with_422(
    client: TestClient, written: _Written,
) -> None:
    resp = _post(client, _ndjson({**HEADER, "format": "graphos.schema"}, ADA))
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "graphos.schema" in resp.json()["detail"]
    assert written.calls == []


def test_a_file_newer_than_the_server_is_refused_with_422(
    client: TestClient, written: _Written,
) -> None:
    resp = _post(client, _ndjson({**HEADER, "version": dataset.VERSION + 1, "graphos": "99.0"},
                                 ADA, END))
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "graphos 99.0" in resp.json()["detail"]
    assert written.calls == []


@pytest.mark.parametrize("version", ["1", 0, 1.5, None])
def test_a_header_with_a_bad_version_is_refused_with_422(
    client: TestClient, written: _Written, version: object,
) -> None:
    resp = _post(client, _ndjson({**HEADER, "version": version}, ADA))
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "positive integer" in resp.json()["detail"]


def test_a_newer_header_later_in_the_file_is_still_refused(
    client: TestClient, written: _Written,
) -> None:
    resp = _post(client, _ndjson(HEADER, ADA, {**HEADER, "version": 2}, BOB))
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert written.calls == []   # the buffered rows were never flushed


def test_an_end_line_whose_counts_disagree_with_the_file_is_not_complete(
    client: TestClient, written: _Written,
) -> None:
    # The end line says 2 nodes and 1 edge; one node line is missing.
    answer = _post(client, _ndjson(HEADER, ADA, KNOWS, END)).json()
    assert answer["complete"] is False


def test_lines_after_the_end_line_are_not_a_complete_file(
    client: TestClient, written: _Written,
) -> None:
    answer = _post(client, _ndjson(HEADER, ADA, END, HEADER, BOB)).json()
    assert answer["complete"] is False
    assert any("after the end line" in e for e in answer["errors"])


# ── Lines that are not written ─────────────────────────────────────────────


def test_a_line_that_is_not_json_is_reported_and_the_rest_imported(
    client: TestClient, written: _Written,
) -> None:
    answer = _post(client, _ndjson(ADA, '{"_kind":"node","id":', BOB)).json()
    assert answer["nodes_upserted"] == 2  # noqa: PLR2004
    assert len(answer["errors"]) == 1
    assert answer["errors"][0].startswith("line 2: ")


@pytest.mark.parametrize("line", ["[1, 2]", "42", '"text"', "null", "true"])
def test_a_json_value_that_is_not_an_object_is_reported(
    client: TestClient, written: _Written, line: str,
) -> None:
    answer = _post(client, _ndjson(line, ADA)).json()
    assert answer["errors"] == ["line 1: expected a JSON object"]
    assert answer["nodes_upserted"] == 1


def test_an_unknown_kind_is_reported_naming_the_known_ones(
    client: TestClient, written: _Written,
) -> None:
    answer = _post(client, _ndjson({"_kind": "relationship", "id": "x"}, ADA)).json()
    assert answer["errors"] == [
        "line 1: unknown `_kind`: 'relationship'; expected 'node', 'edge', 'header' or 'end'"
    ]


def test_a_cut_off_last_line_is_reported_not_imported(
    client: TestClient, written: _Written,
) -> None:
    body = _ndjson(HEADER, ADA, BOB) + json.dumps(KNOWS)[:30]
    resp = _post(client, body)
    assert resp.status_code == HTTPStatus.OK
    answer = resp.json()
    assert answer["complete"] is False
    assert answer["errors"][0].startswith("line 4: ")
    assert answer["edges_received"] == 0


def test_invalid_utf8_is_read_with_replacement_characters(
    client: TestClient, written: _Written,
) -> None:
    line = b'{"_kind":"node","id":"p\xff","label":"Person","properties":{}}\n'
    resp = _post(client, line)
    assert resp.status_code == HTTPStatus.OK
    assert written.nodes("Person") == [{"id": "p�", "props": {}}]


def test_a_node_missing_its_id_or_label_is_reported(client: TestClient, written: _Written) -> None:
    answer = _post(client, _ndjson({"_kind": "node", "label": "Person"},
                                   {"_kind": "node", "id": "x"},
                                   {"_kind": "node", "id": "", "label": "Person"})).json()
    assert [e.split(":")[0] for e in answer["errors"]] == ["line 1", "line 2", "line 3"]
    assert (answer["nodes_received"], answer["nodes_upserted"]) == (3, 0)


def test_a_node_of_an_unregistered_label_is_reported(
    client: TestClient, written: _Written,
) -> None:
    answer = _post(client, _ndjson({**ADA, "label": "Robot"})).json()
    assert answer["errors"] == ["line 1: vertex label 'Robot' not registered"]
    assert written.calls == []


def test_an_edge_of_unknown_labels_or_type_is_reported_naming_them(
    client: TestClient, written: _Written,
) -> None:
    edge = {**KNOWS, "src_label": "Robot", "type": "LIKES"}
    answer = _post(client, _ndjson(ADA, edge)).json()
    assert answer["errors"] == [
        "line 2: unknown vertex labels ['Robot']; unknown edge type ['LIKES']"
    ]
    assert answer["edges_received"] == 1


def test_an_edge_missing_an_end_is_reported(client: TestClient, written: _Written) -> None:
    edge = {k: v for k, v in KNOWS.items() if k != "dst_id"}
    answer = _post(client, _ndjson(edge)).json()
    assert answer["errors"][0].startswith("line 1: ")
    assert "dst_id" in answer["errors"][0]


def test_only_the_first_fifty_errors_are_listed(client: TestClient, written: _Written) -> None:
    answer = _post(client, _ndjson(*(["not json"] * 80), ADA)).json()
    assert len(answer["errors"]) == 50  # noqa: PLR2004
    assert answer["errors"][-1].startswith("line 50: ")
    assert answer["nodes_upserted"] == 1


def test_a_row_against_the_property_schema_is_refused_by_default(
    client: TestClient, written: _Written,
) -> None:
    doc = {"_kind": "node", "id": "d1", "label": "Doc", "properties": {"pages": 3}}
    answer = _post(client, _ndjson(doc)).json()
    assert answer["nodes_upserted"] == 0
    assert answer["errors"][0].startswith("line 1: ")
    assert answer["property_violations"] == {"Doc.title:required": 1}


def test_a_report_import_writes_rows_against_the_property_schema_and_counts_them(
    client: TestClient, written: _Written,
) -> None:
    doc = {"_kind": "node", "id": "d1", "label": "Doc", "properties": {"pages": 3}}
    answer = _post(client, _ndjson(doc), properties="report").json()
    assert (answer["nodes_upserted"], answer["errors"]) == (1, [])
    assert answer["property_warnings"] == {"Doc.title:required": 1}


# ── Order and chunks ───────────────────────────────────────────────────────


def test_buffered_nodes_are_written_before_a_full_chunk_of_edges(
    client: TestClient, written: _Written, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(snapshot, "DEFAULT_SNAPSHOT_MERGE_CHUNK", 2)
    knows_back = {**KNOWS, "src_id": "p2", "dst_id": "p1"}
    _post(client, _ndjson(ADA, KNOWS, BOB, knows_back))
    assert [(kind, name, len(rows)) for kind, name, rows in written.calls] == [
        ("nodes", "Person", 2),          # flushed for the edges, though not full
        ("edges", "Person-KNOWS->Person", 2),
    ]


def test_full_chunks_of_nodes_are_written_as_they_fill(
    client: TestClient, written: _Written, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(snapshot, "DEFAULT_SNAPSHOT_MERGE_CHUNK", 2)
    nodes = [{**ADA, "id": f"p{i}"} for i in range(5)]
    answer = _post(client, _ndjson(*nodes)).json()
    assert [len(rows) for _, _, rows in written.calls] == [2, 2, 1]
    assert answer["nodes_upserted"] == 5  # noqa: PLR2004


def test_edges_are_written_per_pair_of_end_labels_and_type(
    client: TestClient, written: _Written,
) -> None:
    doc = {"_kind": "node", "id": "d1", "label": "Doc", "properties": {"title": "T"}}
    wrote = {"_kind": "edge", "src_id": "p1", "src_label": "Person", "dst_id": "d1",
             "dst_label": "Doc", "type": "WROTE"}
    answer = _post(client, _ndjson(ADA, BOB, doc, KNOWS, wrote)).json()
    assert {name for kind, name, _ in written.calls if kind == "edges"} == {
        "Person-KNOWS->Person", "Person-WROTE->Doc",
    }
    assert answer["by_edge_type"] == {"KNOWS": 1, "WROTE": 1}


# ── Refusals ───────────────────────────────────────────────────────────────


def test_an_unregistered_tenant_is_404(client: TestClient, written: _Written) -> None:
    resp = client.post("/v1/other/graph/snapshot-import", content=_ndjson(ADA), headers=NDJSON)
    assert resp.status_code == HTTPStatus.NOT_FOUND


def test_empty_mode_refuses_a_tenant_holding_nodes_with_409(
    client: TestClient, written: _Written, pool: _Pool,
) -> None:
    pool.holds_nodes = True
    resp = _post(client, _ndjson(ADA), mode="empty")
    assert resp.status_code == HTTPStatus.CONFLICT
    assert "mode=merge" in resp.json()["detail"]
    assert written.calls == []


def test_empty_mode_imports_into_a_tenant_without_nodes(
    client: TestClient, written: _Written, pool: _Pool,
) -> None:
    answer = _post(client, _ndjson(ADA), mode="empty").json()
    assert (answer["mode"], answer["nodes_upserted"]) == ("empty", 1)


def test_an_unknown_mode_is_422(client: TestClient, written: _Written) -> None:
    assert _post(client, _ndjson(ADA), mode="replace").status_code == (
        HTTPStatus.UNPROCESSABLE_ENTITY
    )


def test_a_body_past_the_size_cap_is_413(client: TestClient, written: _Written) -> None:
    body = _ndjson(*({**ADA, "id": f"p{i}"} for i in range(2000)))
    assert len(body) > 64 * 1024  # noqa: PLR2004 -- the premise
    resp = _post(client, body)
    assert resp.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert "GRAPHOS_IMPORT_MAX_BYTES" in resp.json()["detail"]


def test_a_small_gzip_body_that_inflates_past_the_cap_is_413(
    client: TestClient, written: _Written,
) -> None:
    body = gzip.compress(_ndjson(ADA).encode() + b"\n" * (1 << 20))
    assert len(body) < 64 * 1024  # noqa: PLR2004 -- the premise
    resp = _post(client, body)
    assert resp.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


def test_more_lines_than_allowed_is_413(
    client: TestClient, written: _Written, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(snapshot, "MAX_SNAPSHOT_ROWS", 3)
    resp = _post(client, _ndjson(HEADER, ADA, BOB, END))
    assert resp.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert "exceeded 3 rows" in resp.json()["detail"]


def test_a_corrupt_gzip_body_is_a_clean_400(client: TestClient, written: _Written) -> None:
    body = dataset.GZIP_MAGIC + b"\x08\x00this is not deflate data at all" * 10
    resp = _post(client, body)
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert "snapshot import failed (error)" in resp.json()["detail"]


def test_a_write_the_database_refuses_is_400_naming_the_label(
    client: TestClient, written: _Written,
) -> None:
    written.node_error = CypherError("bad agtype")
    resp = _post(client, _ndjson(ADA))
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.json()["detail"] == "cypher error in node label 'Person': bad agtype"


def test_an_edge_write_the_database_refuses_is_400_naming_the_pattern(
    client: TestClient, written: _Written,
) -> None:
    written.edge_error = CypherError("bad agtype")
    resp = _post(client, _ndjson(ADA, BOB, KNOWS))
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.json()["detail"] == "cypher error on -[Person-KNOWS->Person]-: bad agtype"


def test_an_unexpected_failure_is_400_without_its_message(
    client: TestClient, written: _Written,
) -> None:
    written.node_error = RuntimeError("secret internals")
    resp = _post(client, _ndjson(ADA))
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.json()["detail"] == "snapshot import failed (RuntimeError); see server log"


def test_a_unique_property_clash_is_409(client: TestClient, written: _Written) -> None:
    written.node_error = asyncpg.exceptions.UniqueViolationError("duplicate key")
    resp = _post(client, _ndjson(ADA))
    assert resp.status_code == HTTPStatus.CONFLICT


def test_a_statement_timeout_is_504(client: TestClient, written: _Written) -> None:
    written.node_error = asyncpg.exceptions.QueryCanceledError("canceling statement")
    resp = _post(client, _ndjson(ADA))
    assert resp.status_code == HTTPStatus.GATEWAY_TIMEOUT


# ── The export route ───────────────────────────────────────────────────────


@pytest.fixture
def exported(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    for module in (graph, snapshot):
        monkeypatch.setattr(module, "registered_tenants", lambda: {"acme": SPEC})
    asked: dict[str, Any] = {}

    async def load_spec(_pool: object, tenant: str) -> TenantSpec:
        assert tenant == "acme"
        return SPEC

    async def lines(_pool: object, spec: TenantSpec, **kw: Any) -> AsyncIterator[str]:
        asked.update(kw)
        for row in (HEADER, ADA, END):
            yield json.dumps(row) + "\n"

    monkeypatch.setattr(snapshot, "load_spec", load_spec)
    monkeypatch.setattr(dataset, "lines", lines)
    return asked


def test_the_export_is_an_ndjson_attachment(client: TestClient, exported: dict) -> None:
    resp = client.get(EXPORT)
    assert resp.status_code == HTTPStatus.OK
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert resp.headers["content-disposition"] == 'attachment; filename="acme.ndjson"'
    assert [json.loads(x)["_kind"] for x in resp.text.splitlines()] == ["header", "node", "end"]
    assert exported == {"labels": None, "types": None, "computed": True}


def test_a_gzip_export_is_a_gz_attachment_that_decompresses(
    client: TestClient, exported: dict,
) -> None:
    resp = client.get(EXPORT, params={"gzip": "true", "computed": "false",
                                      "labels": ["Person"], "types": ["KNOWS"]})
    assert resp.status_code == HTTPStatus.OK
    assert resp.headers["content-disposition"] == 'attachment; filename="acme.ndjson.gz"'
    assert resp.headers["content-type"] == "application/gzip"
    assert exported == {"labels": ["Person"], "types": ["KNOWS"], "computed": False}


@pytest.mark.parametrize(("params", "named"), [
    ({"labels": ["Person", "Robot"]}, "vertex labels not registered for tenant 'acme': ['Robot']"),
    ({"types": ["LIKES"]}, "edge types not registered for tenant 'acme': ['LIKES']"),
])
def test_an_export_of_unregistered_labels_or_types_is_400(
    client: TestClient, exported: dict, params: dict, named: str,
) -> None:
    resp = client.get(EXPORT, params=params)
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.json()["detail"] == named
    assert exported == {}

"""The Cypher console's pure parts: result columns, agtype values, the graph a
result makes, tenant roles, and the routes while the console is off."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from typing import Any

import pytest
from fastapi.testclient import TestClient

from graphos.app import create_app
from graphos.auth import StaticAuthenticator
from graphos.config import Settings
from graphos.console import (
    MAX_COLUMNS,
    Console,
    _cell,
    _column_counts,
    _Graph,
    _resolve_edges,
    agtype_value,
    needs_age_graph,
    qualified_calls,
    return_columns,
    variable_length,
)
from graphos.identity import Principal


@pytest.mark.parametrize(
    ("query", "columns"),
    [
        ("MATCH (n) RETURN n", ["n"]),
        ("MATCH (n) RETURN n.id, n.k", ["n.id", "n.k"]),
        ("MATCH (n) RETURN n.id AS id, {a: 1, b: [1, 2]} AS m", ["id", "m"]),
        ("MATCH (n) RETURN 'a, b' AS s, n.k", ["s", "n.k"]),
        (
            "MATCH (n)-[]->(m) WITH n, count(*) AS c RETURN n.id, c ORDER BY c DESC LIMIT 5",
            ["n.id", "c"],
        ),
        ("MATCH (n) RETURN DISTINCT n.k LIMIT 3", ["n.k"]),
        ("MATCH (n) RETURN n.id UNION MATCH (m) RETURN m.id", ["m.id"]),
        (
            "MATCH (n) RETURN coalesce(n.x, n.id), substring(n.id, 0, 1)",
            ["coalesce(n.x,n.id)", "substring(n.id,0,1)"],
        ),
        ("MATCH (n) RETURN n.id AS `a, b`", ["a, b"]),
        ("MATCH (n) // RETURN x, y\nRETURN /* RETURN a, b */ n.id", ["n.id"]),
        ("match (n) return n.id, n.k", ["n.id", "n.k"]),
        ("MATCH (n) RETURN [x IN range(1, 3) | x * 2] AS xs, n.id", ["xs", "n.id"]),
        ("MATCH (n) RETURN CASE WHEN n.k > 1 THEN 'a' ELSE 'b' END AS c, n.k", ["c", "n.k"]),
        ("MATCH (n) RETURN n.id SKIP 1 LIMIT 2", ["n.id"]),
    ],
)
def test_return_columns_are_read_from_the_last_top_level_return(
    query: str, columns: list[str]
) -> None:
    assert return_columns(query) == columns


def test_return_star_and_no_return_have_nothing_to_read() -> None:
    assert return_columns("MATCH (n) RETURN *") is None
    assert return_columns("MATCH (n) DELETE n") is None


def test_column_counts_try_the_read_one_first_then_every_other() -> None:
    assert _column_counts(["a", "b"])[:3] == [2, 1, 3]
    assert len(_column_counts(["a", "b"])) == MAX_COLUMNS
    assert _column_counts(None) == list(range(1, MAX_COLUMNS + 1))


def test_agtype_values_mark_vertices_and_edges_and_leave_strings_alone() -> None:
    raw = (
        '[{"id": 1, "label": "Doc", "properties": {"id": "d1", "note": "x::vertex"}}::vertex, '
        '{"id": 9, "label": "LINKS", "end_id": 2, "start_id": 1, "properties": {}}::edge, '
        '{"id": 2, "label": "Doc", "properties": {"id": "d2"}}::vertex]::path'
    )
    path = agtype_value(raw)
    assert [p["__ag"] for p in path] == ["vertex", "edge", "vertex"]
    assert path[0]["properties"]["note"] == "x::vertex"
    assert agtype_value("12::numeric") == 12
    assert agtype_value('"text"') == "text"
    assert agtype_value("null") is None
    assert agtype_value(None) is None
    assert agtype_value('{"a": "{not}::edge"}') == {"a": "{not}::edge"}


def test_a_result_becomes_cells_and_a_graph_of_its_nodes_and_their_edges() -> None:
    graph = _Graph()
    path = agtype_value(
        '[{"id": 1, "label": "Doc", "properties": {"id": "d1"}}::vertex, '
        '{"id": 9, "label": "LINKS", "end_id": 2, "start_id": 1, "properties": {"w": 1}}::edge, '
        '{"id": 2, "label": "Doc", "properties": {"id": "d2"}}::vertex]::path',
    )
    lonely = agtype_value(
        '{"id": 10, "label": "LINKS", "end_id": 77, "start_id": 1, "properties": {}}::edge'
    )
    cells = [_cell(path, graph), _cell(lonely, graph)]
    edges = _resolve_edges(graph)
    assert cells[0][0] == {"node": {"id": "d1", "label": "Doc", "properties": {"id": "d1"}}}
    assert cells[0][1]["edge"]["src_id"] == "d1" and cells[0][1]["edge"]["dst_id"] == "d2"
    assert cells[1]["edge"]["dst_id"] is None  # its other end was not returned
    assert edges == [
        {
            "type": "LINKS",
            "properties": {"w": 1},
            "src_id": "d1",
            "src_label": "Doc",
            "dst_id": "d2",
            "dst_label": "Doc",
        }
    ]
    assert [n["id"] for n in graph.nodes.values()] == ["d1", "d2"]


@pytest.mark.parametrize(
    ("query", "calls"),
    [
        ("RETURN pg_catalog.version()", ["pg_catalog.version"]),
        ("RETURN pg_catalog . version ( )", ["pg_catalog.version"]),
        ("RETURN `pg_catalog`.`version`()", ["pg_catalog.version"]),
        ("RETURN a.b.c(1)", ["b.c"]),
        ("MATCH (n) RETURN n.id, toLower(n.name), size(n.tags)", []),
        ("RETURN 'pg_catalog.version()'", []),
        ("RETURN /* pg_catalog.version() */ 1 // x.y()", []),
        ("MATCH (n) WHERE n.k = 1 RETURN n.name", []),
    ],
)
def test_qualified_calls_are_found_however_written(query: str, calls: list[str]) -> None:
    assert qualified_calls(query) == calls


@pytest.mark.parametrize(
    ("query", "found"),
    [
        ("MATCH p = (a)-[:R*1..3]->(b) RETURN p", True),
        ("MATCH (a)<-[*]-(b) RETURN b", True),
        ("MATCH (a)-[r:R *2]-(b) RETURN b", True),
        ("MATCH (a)-[:R]->()-[:R]->(b) RETURN b", False),
        ("MATCH (a) RETURN count(*), [x IN [1, 2] | x * 2]", False),
        ("MATCH (a) RETURN *", False),
        ("MATCH (a {k: '-[*]-'}) RETURN a // -[*]-", False),
    ],
)
def test_variable_length_patterns_are_found(query: str, found: bool) -> None:  # noqa: FBT001
    assert variable_length(query) is found


def test_what_needs_ages_in_memory_graph_is_named() -> None:
    assert needs_age_graph("MATCH p = (a)-[*1..2]->(b) RETURN p") == (
        "variable-length patterns (-[*1..3]->)"
    )
    assert needs_age_graph("MATCH (n) RETURN VERTEX_STATS(n)") == "VERTEX_STATS()"
    assert needs_age_graph("MATCH (n {k: 'vertex_stats(n)'}) RETURN n.vertex_stats") is None


@pytest.mark.asyncio
async def test_the_watchdog_closes_a_connection_whose_backend_does_not_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AGE's variable-length search sees neither a cancel nor a terminate:
    # the watchdog then closes the connection, so the caller is answered.
    monkeypatch.setattr("graphos.console.WATCHDOG_TERMINATE_AFTER_S", 0.01)
    sent: list[str] = []

    class Owner:
        async def execute(self, sql: str, pid: int) -> None:
            sent.append(sql.split("(", maxsplit=1)[0].removeprefix("SELECT "))

    class Conn:
        closed = False

        def get_server_pid(self) -> int:
            return 42

        def terminate(self) -> None:
            self.closed = True

    async def gone() -> bool:
        return True

    conn = Conn()
    console = Console(pool=None, owner=Owner(), settings=Settings(db_password="x"))  # type: ignore[arg-type]
    stopped: dict[str, Any] = {}
    await asyncio.wait_for(console._watch(conn, gone, stopped), 5)  # type: ignore[arg-type]
    assert sent == ["pg_cancel_backend", "pg_terminate_backend"]
    assert conn.closed and stopped == {"why": "gone"}


def test_every_console_route_answers_404_while_it_is_off() -> None:
    app = create_app(Settings(db_password="placeholder"))
    app.state.authenticator = StaticAuthenticator({"tok": Principal.static("ops", admin=True)})
    app.state.console = None
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer tok"}
    for method, path, body in (
        ("POST", "/v1/acme/cypher", {"query": "MATCH (n) RETURN n"}),
        ("GET", "/v1/acme/cypher/saved", None),
        ("PUT", "/v1/acme/cypher/saved/q1", {"query": "MATCH (n) RETURN n"}),
        ("POST", "/v1/acme/cypher/saved/q1:run", {}),
    ):
        resp = client.request(method, path, headers=headers, json=body)
        assert resp.status_code == HTTPStatus.NOT_FOUND, (method, path, resp.text)
        assert resp.json()["detail"] == "the Cypher console is off"

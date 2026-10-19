"""The Cypher console's pure parts: result columns, agtype values, the graph a
result makes, tenant roles, and the routes while the console is off."""

from __future__ import annotations

import asyncio
import contextlib
import re
from http import HTTPStatus
from pathlib import Path
from typing import Any

import anyio
import asyncpg
import pytest
from fastapi.testclient import TestClient

from graphos import console as console_mod
from graphos.app import create_app
from graphos.auth import StaticAuthenticator
from graphos.config import Settings
from graphos.console import (
    MAX_COLUMNS,
    Console,
    ConsoleError,
    _cell,
    _column_counts,
    _Graph,
    _resolve_edges,
    agtype_value,
    needs_age_graph,
    qualified_calls,
    return_columns,
    unknown_calls,
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
    ("query", "calls"),
    [
        ("MATCH (n:Doc) RETURN toUpper(n.title), count(*), coalesce(n.x, 1)", []),
        ("MATCH (n) WHERE exists(n.x) AND (n)-[:R]->() RETURN collect(DISTINCT n)", []),
        ("MATCH p = (a)-->(b) RETURN length(p), nodes(p), [x IN range(1, 3) | x * 2]", []),
        ("UNWIND [1, 2] AS x WITH x WHERE (x > 1) RETURN CASE WHEN (x = 2) THEN 1 END", []),
        ("RETURN version()", ["version"]),
        ("RETURN similarity('a', 'b'), `set_limit`(0.5)", ["similarity", "set_limit"]),
        ("MATCH (n) RETURN prepare_cypher(n)", ["prepare_cypher"]),        # AGE's internals
        ("RETURN 'version()' // version()", []),                          # strings, comments
        ("RETURN pg_catalog.version()", []),                              # qualified_calls' job
    ],
)
def test_calls_that_are_not_cyphers_are_found(query: str, calls: list[str]) -> None:
    assert unknown_calls(query) == calls


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


@pytest.mark.parametrize(
    ("query", "found"),
    [
        ("CREATE (n:Doc {id: 'x'}) RETURN n", ["CREATE"]),
        ("MERGE (n:Doc {id: 'x'}) ON CREATE SET n.a = 1 RETURN n", ["MERGE", "CREATE", "SET"]),
        ("MATCH (n:Contract {id: 'c'}) SET n.x = 1 RETURN n.id", ["SET"]),
        ("MATCH (n) set n += {a: 1}", ["SET"]),
        ("MATCH (n {id: 'c'}) DETACH DELETE n", ["DETACH", "DELETE"]),
        ("MATCH ()-[r]->() DELETE r", ["DELETE"]),
        ("MATCH (n) REMOVE n.x RETURN n", ["REMOVE"]),
        ("MATCH (n) WITH n /* SET n.x = 1 */ RETURN n.set, n.`delete`", []),
        ("MATCH (n:Set)-[:REMOVE]->(m) RETURN {create: 1, merge: n.id} AS m", []),
        ("MATCH (n) WHERE n.name = 'SET ME' OR n.x = $delete RETURN n // DELETE", []),
        ("MATCH (`create`) RETURN `create`", []),
    ],
)
def test_write_clauses_are_found_and_nothing_else(query: str, found: list[str]) -> None:
    assert console_mod.write_clauses(query) == found


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
    await asyncio.wait_for(console._watch(conn, gone, stopped, asyncio.Event()), 5)  # type: ignore[arg-type]
    assert sent == ["pg_cancel_backend", "pg_terminate_backend"]
    assert conn.closed and stopped == {"why": "gone"}



@pytest.mark.asyncio
async def test_the_watchdog_does_not_fail_on_a_connection_the_terminate_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When pg_terminate_backend did reach the backend, asyncpg has closed the
    # connection and released it to the pool: closing it again is refused,
    # and that must not become the query's answer (it was a 500 after 41 s).
    monkeypatch.setattr("graphos.console.WATCHDOG_TERMINATE_AFTER_S", 0.01)

    class Owner:
        async def execute(self, sql: str, pid: int) -> None:
            pass

    class Released:
        def get_server_pid(self) -> int:
            return 42

        def terminate(self) -> None:
            raise asyncpg.InterfaceError(
                "cannot call Connection.terminate(): connection has been released back to the pool",
            )

    async def gone() -> bool:
        return True

    console = Console(pool=None, owner=Owner(), settings=Settings(db_password="x"))  # type: ignore[arg-type]
    stopped: dict[str, Any] = {}
    await asyncio.wait_for(console._watch(Released(), gone, stopped, asyncio.Event()), 5)  # type: ignore[arg-type]
    assert stopped == {"why": "gone"}


@pytest.mark.asyncio
async def test_a_role_that_may_not_bound_temporary_files_is_told_not_stopped() -> None:
    class Refusing:
        async def execute(self, sql: str) -> None:
            raise asyncpg.InsufficientPrivilegeError(
                'permission denied to set parameter "temp_file_limit"',
            )

        def transaction(self) -> contextlib.AbstractAsyncContextManager[None]:
            return contextlib.nullcontext()

    class Owner:
        def acquire(self) -> contextlib.AbstractAsyncContextManager[Refusing]:
            return contextlib.nullcontext(Refusing())

    assert await console_mod._may_set(Owner(), "temp_file_limit", "1GB") is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_watchdog_stops_when_the_query_ends_even_if_its_cancel_is_lost() -> None:
    # The cancel _on sends when the query ends did not always stop the
    # watchdog (most likely when it landed inside the caller check, Starlette's
    # is_disconnected, run in an anyio scope it cancels itself); the watchdog
    # then went on to its deadline (35 s) and held the answer. Ended is ended:
    # it must stop at once, here with the cancel landing in that check.
    class Owner:
        async def execute(self, sql: str, pid: int) -> None:
            raise AssertionError("an idle backend was stopped")

    class Conn:
        def get_server_pid(self) -> int:
            return 42

        def terminate(self) -> None:
            raise AssertionError("the connection was closed")

    checking = asyncio.Event()

    async def gone() -> bool:  # what Request.is_disconnected does
        checking.set()
        with anyio.CancelScope() as scope:
            scope.cancel()
            await asyncio.sleep(0.1)
        return False

    console = Console(pool=None, owner=Owner(), settings=Settings(db_password="x"))  # type: ignore[arg-type]
    done = asyncio.Event()
    watchdog = asyncio.create_task(console._watch(Conn(), gone, {}, done))  # type: ignore[arg-type]
    await checking.wait()          # the watchdog is inside the caller check
    done.set()
    watchdog.cancel()
    started = asyncio.get_running_loop().time()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(watchdog, 2)
    assert asyncio.get_running_loop().time() - started < 1.0

class _Pool:
    async def acquire(self) -> object:
        return object()

    async def release(self, conn: object) -> None:
        pass


def _timed_console(wait_s: float, run_s: float, ran: list[str]) -> Console:
    console = Console(
        pool=_Pool(), owner=None, settings=Settings(db_password="x", cypher_wait_s=wait_s),  # type: ignore[arg-type]
    )

    async def on(conn: object, tenant: str, text: str, *rest: object) -> dict[str, Any]:
        ran.append(f"start {text}")
        await asyncio.sleep(run_s)
        ran.append(f"end {text}")
        return {"rows": []}

    console._on = on  # type: ignore[method-assign]
    return console


async def _nobody_left() -> bool:
    return False


@pytest.mark.asyncio
async def test_a_users_next_query_waits_its_turn_instead_of_being_refused() -> None:
    # An agent sends its queries together; with one query per user the second
    # used to answer 429 at once.
    ran: list[str] = []
    console = _timed_console(wait_s=5, run_s=0.1, ran=ran)
    first, second = ("MATCH (a) RETURN a", "MATCH (b) RETURN b")
    await asyncio.gather(*(
        console.run("t", q, params=None, limit=None, consumer="agent", gone=_nobody_left)
        for q in (first, second)
    ))
    assert ran == [f"start {first}", f"end {first}", f"start {second}", f"end {second}"]
    assert console._running == {}
    other = await asyncio.gather(*(
        console.run("t", q, params=None, limit=None, consumer=who, gone=_nobody_left)
        for q, who in ((first, "a"), (second, "b"))
    ))
    assert len(other) == 2  # noqa: PLR2004  two users run side by side


@pytest.mark.asyncio
async def test_a_turn_not_given_within_the_wait_is_429() -> None:
    ran: list[str] = []
    console = _timed_console(wait_s=0.05, run_s=0.5, ran=ran)
    results = await asyncio.gather(*(
        console.run("t", q, params=None, limit=None, consumer="agent", gone=_nobody_left)
        for q in ("MATCH (a) RETURN a", "MATCH (b) RETURN b")
    ), return_exceptions=True)
    refused = [r for r in results if isinstance(r, ConsoleError)]
    assert len(refused) == 1 and refused[0].status == 429  # noqa: PLR2004
    assert "did not finish within 0.05 s" in str(refused[0])
    assert console._running == {}


def test_the_console_is_on_unless_it_is_turned_off() -> None:
    assert Settings(db_password="placeholder").cypher is True        # ADR-0006


def test_every_console_route_answers_404_while_it_is_off() -> None:
    app = create_app(Settings(db_password="placeholder", cypher=False))
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


def test_only_the_console_sends_cypher_to_the_database() -> None:
    """With the console off, nothing in graphos runs Cypher: the console is the
    only module that builds a ``cypher(...)`` call, and ``query_cypher`` (a
    reference helper for tests) is used by no server module."""
    src = Path(console_mod.__file__).parent
    builders, users = [], []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"cypher\('\{", text):
            builders.append(path.relative_to(src).as_posix())
        if path.name != "cypher.py" and "query_cypher" in text:
            users.append(path.relative_to(src).as_posix())
    assert sorted(builders) == ["console.py", "cypher.py"]
    assert users == []


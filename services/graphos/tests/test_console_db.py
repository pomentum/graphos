"""DB tests: the Cypher console (``graphos.console``, ``graphos.api.cypher``).

The console reads its tenant's graph only, refuses writes and calls of
database functions, stops a query at its timeout, limits one query per user,
maps vertices, edges and paths, and keeps saved queries, which go with the
tenant's data.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote, urlparse

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import console as console_mod
from graphos.app import create_app
from graphos.auth import StaticAuthenticator
from graphos.bootstrap import forget
from graphos.config import Settings
from graphos.console import APPLICATION_NAME
from graphos.identity import Principal

from .conftest import requires_db

TENANT, OTHER = "graphos_test_console", "graphos_test_console_b"
PRINCIPALS = {
    "reader": Principal.static("rita", tenants={TENANT: "read"}),
    "maintainer": Principal.static("max", tenants={TENANT: "maintain"}),
    "admin": Principal.static("ops", admin=True),
}
ROWS = [
    {"_kind": "node", "id": "d1", "label": "Doc", "properties": {"title": "One", "pages": 3}},
    {"_kind": "node", "id": "d2", "label": "Doc", "properties": {"title": "Two", "pages": 5}},
    {"_kind": "node", "id": "a1", "label": "Author", "properties": {"name": "Ada"}},
    {
        "_kind": "edge",
        "src_id": "a1",
        "src_label": "Author",
        "dst_id": "d1",
        "dst_label": "Doc",
        "type": "WROTE",
        "properties": {"year": 2020},
    },
    {
        "_kind": "edge",
        "src_id": "a1",
        "src_label": "Author",
        "dst_id": "d2",
        "dst_label": "Doc",
        "type": "WROTE",
        "properties": {"year": 2021},
    },
]


def _settings(**extra: Any) -> Settings:
    dsn = urlparse(os.environ["GRAPHOS_TEST_DSN"])
    return Settings(
        db_host=dsn.hostname,
        db_port=dsn.port or 5432,
        db_name=dsn.path.lstrip("/"),
        db_user=unquote(dsn.username or ""),
        db_password=unquote(dsn.password or ""),
        cypher=True,
        **extra,
    )


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for name in (TENANT, OTHER):
            if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", name):
                await conn.execute("SELECT drop_graph($1::name, true);", name)
            for table, column in (
                ("tenants", "name"),
                ("unregistered_tenants", "name"),
                ("saved_queries", "tenant"),
            ):
                await conn.execute(f"DELETE FROM graphos_meta.{table} WHERE {column} = $1;", name)
            forget(name)


def _as(who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {who}"}


async def _api(db_pool: asyncpg.Pool, settings: Settings) -> tuple[httpx.AsyncClient, Any]:
    app = create_app(settings)
    app.state.pool = db_pool
    app.state.authenticator = StaticAuthenticator(PRINCIPALS)
    app.state.console = await console_mod.start(settings, db_pool)
    assert app.state.console is not None
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, app


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _fresh(db_pool)
    client, app = await _api(db_pool, _settings(cypher_timeout_s=2.0))
    async with client:
        for name, labels in ((TENANT, ["Doc", "Author"]), (OTHER, ["Secret"])):
            body = {"name": name, "vertex_labels": labels, "edge_labels": ["WROTE"]}
            assert (
                await client.post("/v1/tenants", json=body, headers=_as("admin"))
            ).status_code == 200
        resp = await client.post(
            f"/v1/{TENANT}/graph/snapshot-import",
            headers=_as("admin"),
            content="".join(json.dumps(r) + "\n" for r in ROWS),
        )
        assert resp.status_code == HTTPStatus.OK, resp.text
        await client.post(
            f"/v1/{OTHER}/graph/nodes",
            headers=_as("admin"),
            json={"nodes": [{"id": "s1", "label": "Secret", "properties": {"token": "x"}}]},
        )
        yield client
    await app.state.console.pool.close()
    await _fresh(db_pool)


async def _query(
    api: httpx.AsyncClient, query: str, who: str = "reader", **body: Any
) -> httpx.Response:
    return await api.post(f"/v1/{TENANT}/cypher", json={"query": query, **body}, headers=_as(who))


async def _console_backends(db_pool: asyncpg.Pool) -> set[int]:
    rows = await db_pool.fetch(
        "SELECT pid FROM pg_stat_activity WHERE application_name = $1;", APPLICATION_NAME,
    )
    return {r["pid"] for r in rows}


@requires_db
@pytest.mark.asyncio
async def test_a_failed_query_does_not_leave_its_connection_for_the_next(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    # A failed AGE call can leave its database process unusable (a lock AGE
    # took and kept): the console closes the connection rather than reuse it.
    assert (await _query(api, "MATCH (d:Doc) RETURN count(d)")).status_code == HTTPStatus.OK
    before = await _console_backends(db_pool)
    assert before
    failed = await _query(api, "MATCH (d:Doc) RETURN d.pages / 0")
    assert failed.status_code == HTTPStatus.BAD_REQUEST, failed.text
    for _ in range(50):  # the close reaches the server shortly after
        if not before & await _console_backends(db_pool):
            break
        await asyncio.sleep(0.1)
    assert not before & await _console_backends(db_pool)
    # The console carries on, on a fresh connection.
    assert (await _query(api, "MATCH (d:Doc) RETURN count(d)")).json()["rows"] == [[2]]
    # What needs AGE's in-memory graph is refused before it runs.
    stats = await _query(api, "MATCH (d:Doc) RETURN vertex_stats(d)")
    assert stats.status_code == HTTPStatus.BAD_REQUEST
    assert "vertex_stats()" in stats.json()["detail"]


@requires_db
@pytest.mark.asyncio
async def test_the_console_has_a_pool_of_its_own(api: httpx.AsyncClient) -> None:
    app = api._transport.app  # type: ignore[attr-defined]
    assert app.state.console.pool is not app.state.pool
    assert app.state.console.pool.get_max_size() == app.state.settings.cypher_pool_size


@requires_db
@pytest.mark.asyncio
async def test_a_query_answers_a_table_and_the_graph_it_makes(api: httpx.AsyncClient) -> None:
    resp = await _query(
        api, "MATCH (a:Author)-[w:WROTE]->(d:Doc) RETURN a, w, d.title AS title ORDER BY d.title"
    )
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert body["columns"] == ["a", "w", "title"]
    assert [row[2] for row in body["rows"]] == ["One", "Two"]
    assert body["rows"][0][0] == {
        "node": {"id": "a1", "label": "Author", "properties": {"id": "a1", "name": "Ada"}}
    }
    assert (
        body["rows"][0][1]["edge"]["type"] == "WROTE"
        and body["rows"][0][1]["edge"]["dst_id"] is None
    )
    assert body["graph"]["nodes"] == [
        {"id": "a1", "label": "Author", "properties": {"id": "a1", "name": "Ada"}}
    ]
    assert body["graph"]["edges"] == []  # the docs were not returned
    paths = (await _query(api, "MATCH p = (a:Author)-[:WROTE]->(d:Doc) RETURN p")).json()
    assert {e["dst_id"] for e in paths["graph"]["edges"]} == {"d1", "d2"}
    assert body["truncated"] is False


@requires_db
@pytest.mark.asyncio
async def test_parameters_limits_and_columns_it_cannot_read(api: httpx.AsyncClient) -> None:
    one = (await _query(api, "MATCH (d:Doc {id: $id}) RETURN d.pages", params={"id": "d2"})).json()
    assert one["rows"] == [[5]]
    cut = (await _query(api, "MATCH (d:Doc) RETURN d.id", limit=1)).json()
    assert len(cut["rows"]) == 1 and cut["truncated"] is True
    star = (await _query(api, "MATCH (d:Doc {id: 'd1'}) RETURN *")).json()
    assert star["columns"] == ["c1"] and star["rows"][0][0]["node"]["id"] == "d1"


@requires_db
@pytest.mark.asyncio
async def test_writes_admin_functions_and_other_tenants_are_refused(
    api: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    write = await _query(api, "CREATE (d:Doc {id: 'x'}) RETURN d")
    assert write.status_code == HTTPStatus.BAD_REQUEST and "only reads" in write.json()["detail"]
    admin_fn = await _query(api, "RETURN pg_catalog.pg_reload_conf()")
    assert admin_fn.status_code == HTTPStatus.BAD_REQUEST
    assert "pg_catalog.pg_reload_conf" in admin_fn.json()["detail"]
    # The other tenant's label is not in this graph...
    assert (await _query(api, "MATCH (s:Secret) RETURN s")).json()["rows"] == []
    # ...and this tenant's role cannot read the other's tables, nor graphos's own.
    # Database functions are refused before the query reaches the database,
    # however the name is spaced or quoted; Cypher's own functions work.
    for call in (
        "RETURN pg_catalog . pg_reload_conf ( )",
        "RETURN `pg_catalog`.version()",
        "MATCH (d:Doc) RETURN public.anything(d.id)",
    ):
        refused = await _query(api, call)
        assert refused.status_code == HTTPStatus.BAD_REQUEST, call
        assert "not database functions" in refused.json()["detail"]
    # Not Cypher's, unqualified: AGE 1.6 resolves other extensions' functions
    # (pg_trgm's similarity()) and 1.7+ any on the search path (version()), so
    # graphos refuses them before the database, whichever AGE runs.
    for call in ("RETURN version()", "RETURN similarity('graph', 'graphs')", "RETURN `version`()"):
        refused = await _query(api, call)
        assert refused.status_code == HTTPStatus.BAD_REQUEST, call
        assert "not one of them" in refused.json()["detail"], call
    own = await _query(api, "MATCH (d:Doc {id: 'd1'}) RETURN toUpper(d.title), size(d.title)")
    assert own.json()["rows"] == [["ONE", 3]]
    syntax = await _query(api, "MATCH (d:Doc RETURN d")
    assert syntax.status_code == HTTPStatus.BAD_REQUEST
    # Variable-length patterns: AGE cannot stop one once it runs.
    vle = await _query(api, "MATCH p = (a:Author)-[:WROTE*1..2]->(d) RETURN p")
    assert vle.status_code == HTTPStatus.BAD_REQUEST
    assert "variable-length patterns" in vle.json()["detail"]
    hops = await _query(api, "MATCH (a:Author)-[:WROTE]->(d:Doc) RETURN count(d)")
    assert hops.status_code == HTTPStatus.OK


@requires_db
@pytest.mark.asyncio
async def test_a_runaway_query_is_stopped_and_a_user_runs_one_at_a_time(
    api: httpx.AsyncClient,
) -> None:
    # 10^12 rows: it would run for hours.
    runaway = "UNWIND range(1, 1000000) AS x UNWIND range(1, 1000000) AS y RETURN count(*)"
    slow = asyncio.create_task(_query(api, runaway))
    await asyncio.sleep(0.5)
    busy = await _query(api, "MATCH (d:Doc) RETURN d.id")
    assert busy.status_code == HTTPStatus.TOO_MANY_REQUESTS
    stopped = await slow
    assert stopped.status_code == HTTPStatus.GATEWAY_TIMEOUT, stopped.text
    # The connection is usable again.
    assert (await _query(api, "MATCH (d:Doc) RETURN count(d)")).json()["rows"] == [[2]]


@requires_db
@pytest.mark.asyncio
async def test_the_level_the_console_needs(api: httpx.AsyncClient) -> None:
    outsider = await api.post(
        f"/v1/{OTHER}/cypher",
        json={"query": "MATCH (s) RETURN s"},
        headers=_as("reader"),
    )
    assert outsider.status_code == HTTPStatus.FORBIDDEN


@requires_db
@pytest.mark.asyncio
async def test_saved_queries_are_defined_by_maintainers_run_by_readers_and_go_with_the_data(
    api: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    saved = {
        "description": "Docs of an author",
        "query": (
            "MATCH (a:Author {id: $author})-[:WROTE]->(d:Doc) "
            "RETURN d.title AS title ORDER BY d.title"
        ),
        "params": {"author": {"type": "string", "label": "Author id"}},
        "show": "table",
    }
    path = f"/v1/{TENANT}/cypher/saved/docs_of"
    assert (
        await api.put(path, json=saved, headers=_as("reader"))
    ).status_code == HTTPStatus.FORBIDDEN
    put = await api.put(path, json=saved, headers=_as("maintainer"))
    assert put.status_code == HTTPStatus.OK and put.json()["created_by"] == "max"
    listed = (await api.get(f"/v1/{TENANT}/cypher/saved", headers=_as("reader"))).json()
    assert [q["name"] for q in listed] == ["docs_of"]
    run = await api.post(f"{path}:run", json={"params": {"author": "a1"}}, headers=_as("reader"))
    assert run.json()["rows"] == [["One"], ["Two"]]
    for params in ({}, {"author": 1}, {"author": "a1", "x": 1}):
        bad = await api.post(f"{path}:run", json={"params": params}, headers=_as("reader"))
        assert bad.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, params
    gone = await api.delete(f"/v1/tenants/{TENANT}?drop_data=true", headers=_as("admin"))
    assert gone.status_code == HTTPStatus.OK
    assert not await db_pool.fetchval(
        "SELECT count(*) FROM graphos_meta.saved_queries WHERE tenant = $1;",
        TENANT,
    )


@requires_db
@pytest.mark.asyncio
async def test_a_caller_who_goes_away_stops_the_query(api: httpx.AsyncClient) -> None:
    console = api._transport.app.state.console  # type: ignore[attr-defined]

    async def gone() -> bool:  # the browser closed the request (the UI's Cancel)
        return True

    started = asyncio.get_running_loop().time()
    with pytest.raises(console_mod.ConsoleError) as refused:
        await console.run(
            TENANT,
            "UNWIND range(1, 1000000) AS x UNWIND range(1, 1000000) AS y RETURN count(*)",
            params=None,
            limit=None,
            consumer="rita",
            gone=gone,
        )
    assert refused.value.status == 499
    # Cancelled at once, or terminated by the watchdog when AGE is slow to see it.
    assert asyncio.get_running_loop().time() - started < 6.0

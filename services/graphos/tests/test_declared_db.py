"""DB tests: declared property indexes and generated columns, end to end.

A registration declares them and graphos writes the DDL: the indexes serve
the ``where`` filters, the columns hold the right values (``NULL`` for a value
of another type, never a failed write), a unique index refuses a duplicate
with 409, and specs stored before 0.6.0 (SQL statements) are converted when
the server starts.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos.app import create_app
from graphos.bootstrap import bootstrap_tenant, forget, load_registered_tenants, registered_tenants
from graphos.config import Settings

from .conftest import requires_db

TENANT = "graphos_test_declared"
SPEC: dict[str, Any] = {
    "name": TENANT,
    "vertex_labels": ["Doc", "User"],
    "edge_labels": ["WROTE"],
    "property_indexes": [
        {"label": "Doc", "properties": ["kind", "owner"]},
        {"label": "Doc", "properties": ["title"], "kind": "trigram"},
        {"label": "User", "properties": ["email"], "unique": True},
    ],
    "generated_columns": [
        {"label": "Doc", "column": "kind", "property": "kind"},
        {"label": "Doc", "column": "score", "property": "score", "type": "numeric"},
        {"label": "Doc", "column": "draft", "property": "draft", "type": "boolean"},
        {"label": "Doc", "column": "tags", "property": "tags", "type": "jsonb"},
    ],
}


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", TENANT):
            await conn.execute(f"SELECT drop_graph('{TENANT}', true);")
        await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", TENANT)
    forget(TENANT)


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _fresh(db_pool)
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        yield client
    await _fresh(db_pool)


async def _indexes(pool: asyncpg.Pool, table: str) -> dict[str, str]:
    rows = await pool.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = $1 AND tablename = $2;",
        TENANT, table,
    )
    return {r["indexname"]: r["indexdef"] for r in rows}


@requires_db
@pytest.mark.asyncio
async def test_declared_indexes_and_columns_are_built(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    resp = await api.post("/v1/tenants", json=SPEC)
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json()["n_property_indexes"] == 3
    assert resp.json()["n_generated_columns"] == 4

    doc = await _indexes(db_pool, "Doc")
    assert "USING btree" in doc["Doc_kind_owner_btree_idx"]
    assert "gin_trgm_ops" in doc["Doc_title_trgm_idx"]
    user = await _indexes(db_pool, "User")
    assert user["User_email_unique_idx"].startswith("CREATE UNIQUE INDEX")

    info = (await api.get(f"/v1/tenants/{TENANT}")).json()
    assert {(x["label"], tuple(x["properties"])) for x in info["property_indexes"]} == {
        ("Doc", ("kind", "owner")), ("Doc", ("title",)), ("User", ("email",)),
    }
    assert [c["column"] for c in info["generated_columns"]] == ["kind", "score", "draft", "tags"]

    # The where filters compile to the indexed expression: the planner uses it.
    async with db_pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL enable_seqscan = off;")
        plan = "\n".join(r[0] for r in await conn.fetch(
            f'EXPLAIN SELECT 1 FROM "{TENANT}"."Doc" v '
            "WHERE ((((v.properties)::text)::jsonb) ->> 'kind') = 'guide' "
            "AND ((((v.properties)::text)::jsonb) ->> 'owner') = 'u1';"
        ))
    assert "Doc_kind_owner_btree_idx" in plan, plan
    # And /graph/search?property=title (no index) matches by substring on it.
    async with db_pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL enable_seqscan = off;")
        plan = "\n".join(r[0] for r in await conn.fetch(
            f'EXPLAIN SELECT 1 FROM "{TENANT}"."Doc" v '
            "WHERE ((((v.properties)::text)::jsonb) ->> 'title') ILIKE '%' || 'guide' || '%';"
        ))
    assert "Doc_title_trgm_idx" in plan, plan

    # Registering again changes nothing and builds nothing twice.
    assert (await api.post("/v1/tenants", json=SPEC)).status_code == HTTPStatus.OK
    assert await _indexes(db_pool, "Doc") == doc


@requires_db
@pytest.mark.asyncio
async def test_generated_columns_never_fail_a_write(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    assert (await api.post("/v1/tenants", json=SPEC)).status_code == HTTPStatus.OK
    nodes = [
        {"id": "d1", "label": "Doc",
         "properties": {"kind": "guide", "score": 2.5, "draft": True, "tags": ["a", "b"]}},
        # Values of other JSON types than the columns': NULL, and still written.
        {"id": "d2", "label": "Doc",
         "properties": {"kind": 7, "score": "high", "draft": "yes", "tags": "x"}},
        {"id": "d3", "label": "Doc", "properties": {}},
    ]
    resp = await api.post(f"/v1/{TENANT}/graph/nodes", json={"nodes": nodes})
    assert resp.status_code == HTTPStatus.OK, resp.text
    rows = await db_pool.fetch(
        f"SELECT trim(both '\"' from (properties -> '\"id\"')::text) AS id, "
        f'kind, score, draft, tags::text AS tags FROM "{TENANT}"."Doc" ORDER BY 1;'
    )
    got = {r["id"]: (r["kind"], r["score"], r["draft"], r["tags"]) for r in rows}
    assert got["d1"][0] == "guide" and float(got["d1"][1]) == 2.5 and got["d1"][2] is True  # noqa: PLR2004
    assert json.loads(got["d1"][3]) == ["a", "b"]
    assert got["d2"] == ("7", None, None, '"x"')
    assert got["d3"] == (None, None, None, None)


@requires_db
@pytest.mark.asyncio
async def test_a_unique_index_refuses_a_duplicate_with_409(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    assert (await api.post("/v1/tenants", json=SPEC)).status_code == HTTPStatus.OK
    user = {"label": "User", "properties": {"email": "a@example.org"}}
    ok = await api.post(f"/v1/{TENANT}/graph/nodes", json={"nodes": [{"id": "u1", **user}]})
    assert ok.status_code == HTTPStatus.OK, ok.text
    dup = await api.post(f"/v1/{TENANT}/graph/nodes", json={"nodes": [{"id": "u2", **user}]})
    assert dup.status_code == HTTPStatus.CONFLICT, dup.text
    assert "User_email_unique_idx" in dup.json()["detail"]
    assert await db_pool.fetchval(f'SELECT count(*) FROM "{TENANT}"."User";') == 1


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "extra"), [
    ("nodes:bulk", {}),
    ("snapshot-import", {"_kind": "node"}),
])
async def test_a_streamed_duplicate_is_refused_with_409(
    api: httpx.AsyncClient, path: str, extra: dict[str, Any],
) -> None:
    # The streaming endpoints answer it as the single-batch one does, not 400.
    assert (await api.post("/v1/tenants", json=SPEC)).status_code == HTTPStatus.OK
    body = "".join(
        json.dumps({**extra, "id": uid, "label": "User",
                    "properties": {"email": "a@example.org"}}) + "\n"
        for uid in ("u1", "u2")
    )
    resp = await api.post(
        f"/v1/{TENANT}/graph/{path}", content=body,
        headers={"content-type": "application/x-ndjson"},
    )
    assert resp.status_code == HTTPStatus.CONFLICT, resp.text
    assert "User_email_unique_idx" in resp.json()["detail"]


@requires_db
@pytest.mark.asyncio
async def test_a_unique_index_over_shared_values_is_refused_with_422(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    assert (await api.post("/v1/tenants", json=SPEC)).status_code == HTTPStatus.OK
    nodes = [{"id": f"d{i}", "label": "Doc", "properties": {"owner": "u1"}} for i in (1, 2)]
    written = await api.post(f"/v1/{TENANT}/graph/nodes", json={"nodes": nodes})
    assert written.status_code == HTTPStatus.OK
    resp = await api.post("/v1/tenants", json={
        "name": TENANT,
        "property_indexes": [{"label": "Doc", "properties": ["owner"], "unique": True}],
    })
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, resp.text
    assert "already share" in resp.text
    # The failed build left no invalid index, and the spec did not take it.
    assert "Doc_owner_unique_idx" not in await _indexes(db_pool, "Doc")
    info = (await api.get(f"/v1/tenants/{TENANT}")).json()
    assert len(info["property_indexes"]) == 3  # noqa: PLR2004


@requires_db
@pytest.mark.asyncio
async def test_an_older_client_that_sent_no_sql_still_registers(
    api: httpx.AsyncClient,
) -> None:
    # The body a client before 0.6.0 sends: both SQL fields, empty.
    resp = await api.post("/v1/tenants", json={
        "name": TENANT, "vertex_labels": ["Doc"], "edge_labels": ["WROTE"],
        "indexes": [], "generated_columns": [], "label_hierarchy": {}, "meta": {},
    })
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert resp.json()["n_property_indexes"] == 0


@requires_db
@pytest.mark.asyncio
async def test_refused_registrations(api: httpx.AsyncClient) -> None:
    assert (await api.post("/v1/tenants", json=SPEC)).status_code == HTTPStatus.OK
    for body, needle in (
        ({"indexes": ['CREATE INDEX IF NOT EXISTS x ON "t"."Doc" (id)']}, "property_indexes"),
        ({"property_indexes": [{"label": "Nope", "properties": ["a"]}]}, "not a vertex label"),
        ({"property_indexes": [{"label": "WROTE", "properties": ["a"]}]}, "not a vertex label"),
        ({"generated_columns": [{"label": "Doc", "column": "score", "property": "score",
                                 "type": "text"}]}, "cannot change"),
    ):
        resp = await api.post("/v1/tenants", json={"name": TENANT, **body})
        assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, (body, resp.text)
        assert needle in resp.text, resp.text


@requires_db
@pytest.mark.asyncio
async def test_a_spec_stored_before_0_6_0_is_converted_at_startup(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    # A tenant set up by an older server: its SQL ran, and the statements are
    # what its stored spec holds.
    assert (await api.post("/v1/tenants", json={
        "name": TENANT, "vertex_labels": ["Doc"], "edge_labels": ["WROTE"],
    })).status_code == HTTPStatus.OK
    legacy_index = (
        f'CREATE INDEX IF NOT EXISTS doc_kind ON "{TENANT}"."Doc" '
        "(((properties::text)::jsonb ->> 'kind'))"
    )
    partial = (
        f'CREATE INDEX IF NOT EXISTS doc_kind_partial ON "{TENANT}"."Doc" '
        "(((properties::text)::jsonb ->> 'kind')) WHERE (properties::text) <> ''"
    )
    legacy_column = (
        f'ALTER TABLE "{TENANT}"."Doc" ADD COLUMN kind text '
        "GENERATED ALWAYS AS (((properties::text)::jsonb ->> 'kind')) STORED"
    )
    async with db_pool.acquire() as conn:
        for stmt in (legacy_index, partial, legacy_column):
            await conn.execute(stmt)
        stored = json.loads(await conn.fetchval(
            "SELECT spec::text FROM graphos_meta.tenants WHERE name = $1;", TENANT,
        ))
        stored.pop("property_indexes")
        stored["indexes"] = [legacy_index, partial]
        stored["generated_columns"] = [legacy_column]
        await conn.execute(
            "UPDATE graphos_meta.tenants SET spec = $2::jsonb WHERE name = $1;",
            TENANT, json.dumps(stored),
        )

    await load_registered_tenants(db_pool)

    spec = registered_tenants()[TENANT]
    assert spec.property_indexes == ({
        "label": "Doc", "properties": ["kind"], "kind": "equality", "unique": False,
        "name": "doc_kind",
    },)
    assert spec.generated_columns == (
        {"label": "Doc", "column": "kind", "property": "kind", "type": "text"},
    )
    row = json.loads(await db_pool.fetchval(
        "SELECT spec::text FROM graphos_meta.tenants WHERE name = $1;", TENANT,
    ))
    assert "indexes" not in row
    # The converted index keeps its name, so bootstrap builds nothing new; the
    # partial index stays in Postgres although the spec left it out.
    before = await _indexes(db_pool, "Doc")
    await bootstrap_tenant(db_pool, spec)
    assert await _indexes(db_pool, "Doc") == before
    assert {"doc_kind", "doc_kind_partial"} <= set(before)

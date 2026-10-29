"""DB tests: a tenant unregistered, restored and deleted (``graphos.lifecycle``),
and the web UI preferences (``graphos.preferences``).

Unregistering keeps the data and the registration, so restoring brings the
tenant back as it was; registering the name again restores it for its owner
and is refused to anyone else; deleting with its data removes the graph and
graphos's rows about it, and frees the name.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from graphos import lifecycle, schema_version
from graphos.app import create_app
from graphos.auth import StaticAuthenticator
from graphos.bootstrap import forget, registered_tenants
from graphos.config import Settings
from graphos.identity import Principal

from .conftest import requires_db

NAME = "graphos_test_life"
SCHEMA: dict[str, Any] = {
    "name": NAME,
    "vertex_labels": ["Doc", "Person"],
    "edge_labels": ["WROTE"],
    "edge_endpoints": {"WROTE": [{"src": "Person", "dst": "Doc"}]},
    "strict_endpoints": True,
}
ROWS = [
    {"_kind": "node", "id": "p1", "label": "Person", "properties": {"name": "Ada"}},
    {"_kind": "node", "id": "d1", "label": "Doc", "properties": {"title": "Notes"}},
    {"_kind": "edge", "src_id": "p1", "src_label": "Person", "dst_id": "d1",
     "dst_label": "Doc", "type": "WROTE", "properties": {}},
]
PRINCIPALS = {
    "owner": Principal.static("olive", tenants={NAME: "owner"}),
    "creator": Principal.static("svc", create_tenants=["graphos_test_*"]),
    "admin": Principal.static("ops", admin=True),
}


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", NAME):
            await conn.execute("SELECT drop_graph($1::name, true);", NAME)
        for table, column in (
            ("tenants", "name"), ("unregistered_tenants", "name"),
            ("edge_endpoints", "tenant"), ("tenant_settings", "tenant"),
            ("changes", "tenant"), ("change_horizon", "tenant"), ("jobs", "tenant"),
        ):
            await conn.execute(f"DELETE FROM graphos_meta.{table} WHERE {column} = $1;", NAME)
        await conn.execute(
            "DELETE FROM graphos_meta.user_preferences WHERE subject = 'user:olive';",
        )
    forget(NAME)


@pytest_asyncio.fixture
async def api(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    await _fresh(db_pool)
    app = create_app(Settings(db_password="placeholder"))
    app.state.pool = db_pool
    app.state.authenticator = StaticAuthenticator(PRINCIPALS)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post("/v1/tenants", json=SCHEMA, headers=_as("admin"))
        assert resp.status_code == HTTPStatus.OK, resp.text
        resp = await client.post(
            f"/v1/{NAME}/graph/snapshot-import", headers=_as("admin"),
            content="".join(json.dumps(r) + "\n" for r in ROWS),
        )
        assert resp.json()["edges_upserted"] == 1, resp.text
        yield client
    await _fresh(db_pool)


def _as(who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {who}"}


async def _counts(api: httpx.AsyncClient) -> tuple[int, int]:
    resp = await api.get(f"/v1/{NAME}/graph/stats", headers=_as("owner"))
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()["n_vertices"], resp.json()["n_edges"]


@requires_db
@pytest.mark.asyncio
async def test_unregistered_keeps_data_and_registration_and_restores_as_it_was(
    api: httpx.AsyncClient,
) -> None:
    resp = await api.delete(f"/v1/tenants/{NAME}", headers=_as("owner"))
    assert resp.json() == {"tenant": NAME, "status": "unregistered"}
    assert NAME not in registered_tenants()
    assert (await api.get(f"/v1/{NAME}/graph/stats", headers=_as("owner"))).status_code == 404

    # Its owner sees it among the unregistered; someone who may only create does not.
    listed = (await api.get("/v1/tenants?unregistered=true", headers=_as("owner"))).json()
    assert [(u["name"], u["kind"], u["vertex_labels"]) for u in listed["unregistered"]] == [
        (NAME, "unregistered", 2),
    ]
    listed = (await api.get("/v1/tenants?unregistered=true", headers=_as("creator"))).json()
    assert NAME not in [u["name"] for u in listed["unregistered"]]
    plain = (await api.get("/v1/tenants", headers=_as("owner"))).json()
    assert "unregistered" not in plain

    resp = await api.post(f"/v1/tenants/{NAME}/restore", headers=_as("owner"))
    assert resp.json() == {"tenant": NAME, "status": "restored"}
    assert await _counts(api) == (2, 1)
    spec = registered_tenants()[NAME]
    assert spec.edge_endpoints == (("WROTE", "Person", "Doc"),) and spec.strict_endpoints
    again = await api.post(f"/v1/tenants/{NAME}/restore", headers=_as("owner"))
    assert again.status_code == HTTPStatus.CONFLICT


@requires_db
@pytest.mark.asyncio
async def test_registering_an_unregistered_name_restores_it_only_for_its_owner(
    api: httpx.AsyncClient,
) -> None:
    await api.delete(f"/v1/tenants/{NAME}", headers=_as("owner"))
    body = {"name": NAME, "vertex_labels": ["Topic"]}
    refused = await api.post("/v1/tenants", json=body, headers=_as("creator"))
    assert refused.status_code == HTTPStatus.CONFLICT
    assert "keeps its data" in refused.json()["detail"]

    dry = await api.post("/v1/tenants?dry_run=true", json=body, headers=_as("owner"))
    assert (dry.json()["exists"], dry.json()["restores"]) == (False, True)
    assert dry.json()["changes"]["added"]["vertex_labels"] == ["Topic"]

    done = await api.post("/v1/tenants", json=body, headers=_as("owner"))
    assert done.status_code == HTTPStatus.OK, done.text
    assert set(done.json()["vertex_labels"]) == {"Doc", "Person", "Topic"}
    assert await _counts(api) == (2, 1)


@requires_db
@pytest.mark.asyncio
async def test_deleting_with_its_data_removes_everything_and_frees_the_name(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    await api.put(f"/v1/{NAME}/cache/k", json={"value": 1}, headers=_as("owner"))
    await api.delete(f"/v1/tenants/{NAME}", headers=_as("owner"))   # unregistered first
    resp = await api.delete(f"/v1/tenants/{NAME}?drop_data=true", headers=_as("owner"))
    assert resp.json() == {"tenant": NAME, "status": "deleted"}
    async with db_pool.acquire() as conn:
        assert not await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", NAME)
        for table, column in (
            ("tenants", "name"), ("unregistered_tenants", "name"), ("cache", "tenant"),
            ("edge_endpoints", "tenant"),
        ):
            assert not await conn.fetchval(
                f"SELECT count(*) FROM graphos_meta.{table} WHERE {column} = $1;", NAME,
            ), table
        # Only the record of the deletion is left in its feed.
        details = await conn.fetch(
            "SELECT detail::text FROM graphos_meta.changes WHERE tenant = $1;", NAME,
        )
        assert [json.loads(r["detail"]) for r in details] == [{"data_deleted": True}]
    gone = await api.delete(f"/v1/tenants/{NAME}?drop_data=true", headers=_as("owner"))
    assert gone.status_code == HTTPStatus.NOT_FOUND
    # The name is free: a new tenant starts empty.
    created = await api.post("/v1/tenants", json={"name": NAME}, headers=_as("creator"))
    assert created.status_code == HTTPStatus.OK, created.text
    assert await _counts(api) == (0, 0)


@requires_db
@pytest.mark.asyncio
async def test_a_registered_tenant_is_deleted_at_once_unless_a_job_runs_on_it(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO graphos_meta.jobs (kind, tenant) VALUES ('vacuum', $1);", NAME,
        )
    busy = await api.delete(f"/v1/tenants/{NAME}?drop_data=true", headers=_as("owner"))
    assert busy.status_code == HTTPStatus.CONFLICT and "job" in busy.json()["detail"]
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM graphos_meta.jobs WHERE tenant = $1;", NAME)
    done = await api.delete(f"/v1/tenants/{NAME}?drop_data=true", headers=_as("owner"))
    assert done.json()["status"] == "deleted"
    assert NAME not in registered_tenants()


@requires_db
@pytest.mark.asyncio
async def test_an_orphan_graph_is_only_an_admins(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    # A graph unregistered the old way: no registration kept.
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", NAME)
    forget(NAME)
    refused = await api.post("/v1/tenants", json={"name": NAME}, headers=_as("creator"))
    assert refused.status_code == HTTPStatus.CONFLICT
    listed = (await api.get("/v1/tenants?unregistered=true", headers=_as("admin"))).json()
    assert {"name": NAME, "kind": "orphan"}.items() <= next(
        u for u in listed["unregistered"] if u["name"] == NAME
    ).items()
    restore = await api.post(f"/v1/tenants/{NAME}/restore", headers=_as("owner"))
    assert restore.status_code == HTTPStatus.CONFLICT
    owner_drop = await api.delete(f"/v1/tenants/{NAME}?drop_data=true", headers=_as("owner"))
    assert owner_drop.status_code == HTTPStatus.FORBIDDEN
    adopted = await api.post("/v1/tenants", json=SCHEMA, headers=_as("admin"))
    assert adopted.status_code == HTTPStatus.OK
    assert await _counts(api) == (2, 1)


@requires_db
@pytest.mark.asyncio
async def test_preferences_keep_only_known_names_and_values(api: httpx.AsyncClient) -> None:
    assert (await api.get("/v1/me", headers=_as("owner"))).json()["preferences"] == {}
    resp = await api.patch(
        "/v1/me/preferences", json={"theme": "dark", "last_tenant": NAME}, headers=_as("owner"),
    )
    assert resp.json() == {"theme": "dark", "last_tenant": NAME}
    for bad in ({"note": "hello"}, {"theme": "blue"}, {"theme": ["dark"]},
                {"last_tenant": "not a name"}):
        refused = await api.patch("/v1/me/preferences", json=bad, headers=_as("owner"))
        assert refused.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, bad
    resp = await api.patch("/v1/me/preferences", json={"theme": None}, headers=_as("owner"))
    assert resp.json() == {"last_tenant": NAME}
    me = (await api.get("/v1/me", headers=_as("owner"))).json()
    assert me["preferences"] == {"last_tenant": NAME}
    # Someone else's are their own.
    assert (await api.get("/v1/me", headers=_as("admin"))).json()["preferences"] == {}


async def _kept(pool: asyncpg.Pool) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        "SELECT * FROM graphos_meta.unregistered_tenants WHERE name = $1;", NAME,
    )
    return None if row is None else dict(row)


@requires_db
@pytest.mark.asyncio
async def test_a_restoring_registration_that_fails_restores_nothing(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    # Two people of one name: a unique index on it fails in bootstrap, on the data.
    twin = {"_kind": "node", "id": "p2", "label": "Person", "properties": {"name": "Ada"}}
    resp = await api.post(f"/v1/{NAME}/graph/snapshot-import", headers=_as("admin"),
                          content=json.dumps(twin) + "\n")
    assert resp.status_code == HTTPStatus.OK, resp.text
    assert (await api.delete(f"/v1/tenants/{NAME}", headers=_as("owner"))).status_code == 200
    kept = await _kept(db_pool)
    assert kept is not None

    unique = {"name": NAME, "property_indexes": [
        {"label": "Person", "properties": ["name"], "unique": True},
    ]}
    resp = await api.post("/v1/tenants", json=unique, headers=_as("owner"))
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, resp.text
    assert "already share these values" in resp.json()["detail"]

    # Still unregistered, kept exactly as it was, and no "restored" record.
    assert NAME not in registered_tenants()
    assert (await api.get(f"/v1/{NAME}/graph/stats", headers=_as("owner"))).status_code == 404
    assert await _kept(db_pool) == kept
    restored = await db_pool.fetchval(
        "SELECT count(*) FROM graphos_meta.changes WHERE tenant = $1 "
        "AND detail::text LIKE '%restored%';", NAME,
    )
    assert restored == 0
    resp = await api.post(f"/v1/tenants/{NAME}/restore", headers=_as("owner"))
    assert resp.json() == {"tenant": NAME, "status": "restored"}
    assert await _counts(api) == (3, 1)
    assert registered_tenants()[NAME].strict_endpoints is True


@requires_db
@pytest.mark.asyncio
async def test_unregister_restore_and_delete_wait_for_a_schema_write(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    async def blocked(call: Any) -> httpx.Response:
        # A schema write (a registration, say) holds the tenant's lock meanwhile.
        async with schema_version.write(db_pool, NAME, None):
            task = asyncio.ensure_future(call())
            await asyncio.sleep(0.5)
            assert not task.done(), "it went ahead under another schema write"
        return await task

    gone = await blocked(lambda: api.delete(f"/v1/tenants/{NAME}", headers=_as("owner")))
    assert gone.json() == {"tenant": NAME, "status": "unregistered"}
    back = await blocked(lambda: api.post(f"/v1/tenants/{NAME}/restore", headers=_as("owner")))
    assert back.json() == {"tenant": NAME, "status": "restored"}
    dropped = await blocked(lambda: api.delete(
        f"/v1/tenants/{NAME}", params={"drop_data": "true"}, headers=_as("owner"),
    ))
    assert dropped.json() == {"tenant": NAME, "status": "deleted"}


async def _hold_advisory(pool: asyncpg.Pool) -> asyncpg.pool.PoolConnectionProxy:
    """The schema-write lock as another replica holds it: the advisory lock
    alone, not this process's own lock."""
    conn = await pool.acquire()
    await conn.execute("SELECT pg_advisory_lock(hashtextextended($1, 0));",
                       "graphos.schema-write:" + NAME)
    return conn


async def _release_advisory(pool: asyncpg.Pool, conn: asyncpg.pool.PoolConnectionProxy) -> None:
    await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0));",
                       "graphos.schema-write:" + NAME)
    await pool.release(conn)


@requires_db
@pytest.mark.asyncio
async def test_another_replicas_schema_write_is_waited_for(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    conn = await _hold_advisory(db_pool)
    try:
        task = asyncio.ensure_future(api.delete(f"/v1/tenants/{NAME}", headers=_as("owner")))
        await asyncio.sleep(0.5)
        assert not task.done(), "it went ahead under another replica's schema write"
    finally:
        await _release_advisory(db_pool, conn)
    assert (await task).json() == {"tenant": NAME, "status": "unregistered"}


@requires_db
@pytest.mark.asyncio
async def test_a_schema_write_held_too_long_answers_409(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schema_version, "LIFECYCLE_WAIT_S", 0.3)
    conn = await _hold_advisory(db_pool)
    try:
        for call in (
            lambda: api.delete(f"/v1/tenants/{NAME}", headers=_as("owner")),
            lambda: api.delete(f"/v1/tenants/{NAME}", params={"drop_data": "true"},
                               headers=_as("owner")),
            lambda: api.post(f"/v1/tenants/{NAME}/restore", headers=_as("owner")),
        ):
            resp = await call()
            assert resp.status_code == HTTPStatus.CONFLICT, resp.text
            assert resp.json()["code"] == "schema_busy"
    finally:
        await _release_advisory(db_pool, conn)
    assert NAME in registered_tenants()   # nothing happened meanwhile


@requires_db
@pytest.mark.asyncio
async def test_a_failure_after_the_registration_committed_keeps_the_restore(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*_args: object) -> None:
        raise RuntimeError("index build failed")

    monkeypatch.setattr("graphos.api.tenants.ensure_search_indexes", broken)
    assert (await api.delete(f"/v1/tenants/{NAME}", headers=_as("owner"))).status_code == 200
    body = {"name": NAME, "search_indexes": {"names": {"labels": ["Person"],
                                                       "properties": ["name"]}}}
    with pytest.raises(RuntimeError):
        await api.post("/v1/tenants", json=body, headers=_as("owner"))
    # The registration and its records stand: the tenant is restored, not put back.
    assert await _kept(db_pool) is None
    restored = await db_pool.fetchval(
        "SELECT count(*) FROM graphos_meta.changes WHERE tenant = $1 "
        "AND detail::text LIKE '%restored%';", NAME,
    )
    assert restored == 1
    assert (await api.get(f"/v1/{NAME}/schema", headers=_as("owner"))).status_code == 200


@requires_db
@pytest.mark.asyncio
async def test_a_failed_undo_keeps_the_registrations_own_answer(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def down(*_args: object) -> None:
        raise ConnectionError("database restarting")

    monkeypatch.setattr(lifecycle, "put_back", down)
    twin = {"_kind": "node", "id": "p2", "label": "Person", "properties": {"name": "Ada"}}
    await api.post(f"/v1/{NAME}/graph/snapshot-import", headers=_as("admin"),
                   content=json.dumps(twin) + "\n")
    assert (await api.delete(f"/v1/tenants/{NAME}", headers=_as("owner"))).status_code == 200
    unique = {"name": NAME, "property_indexes": [
        {"label": "Person", "properties": ["name"], "unique": True},
    ]}
    resp = await api.post("/v1/tenants", json=unique, headers=_as("owner"))
    # The bootstrap's 422, not the undo's failure (which is logged).
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, resp.text


@requires_db
@pytest.mark.asyncio
async def test_if_none_match_registers_only_a_new_tenant(
    api: httpx.AsyncClient,
) -> None:
    star = {**_as("admin"), "If-None-Match": "*"}
    for query in ("?dry_run=true", ""):
        resp = await api.post(f"/v1/tenants{query}", json={"name": NAME}, headers=star)
        assert resp.status_code == HTTPStatus.PRECONDITION_FAILED, resp.text
        assert "is registered" in resp.json()["detail"] and resp.headers["ETag"]
    bad = await api.post("/v1/tenants", json={"name": NAME},
                         headers={**_as("admin"), "If-None-Match": '"v1"'})
    assert bad.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, bad.text
    both = await api.post("/v1/tenants", json={"name": NAME},
                          headers={**star, "If-Match": resp.headers["ETag"]})
    assert both.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, both.text
    assert "not both" in both.json()["detail"]

    # An unregistered tenant is not new: If-None-Match: * never restores it.
    assert (await api.delete(f"/v1/tenants/{NAME}", headers=_as("owner"))).status_code == 200
    for query in ("?dry_run=true", ""):
        resp = await api.post(f"/v1/tenants{query}", json={"name": NAME}, headers=star)
        assert resp.status_code == HTTPStatus.PRECONDITION_FAILED, resp.text
        assert "would restore it" in resp.json()["detail"]
    assert NAME not in registered_tenants()

    # Two creators of a new name at once: one creates it, the other is refused.
    gone = await api.delete(f"/v1/tenants/{NAME}", params={"drop_data": "true"},
                            headers=_as("admin"))
    assert gone.status_code == HTTPStatus.OK, gone.text
    first, second = await asyncio.gather(*(
        api.post("/v1/tenants", json={"name": NAME, "vertex_labels": ["Doc"]}, headers=star)
        for _ in range(2)
    ))
    assert sorted((first.status_code, second.status_code)) == [
        HTTPStatus.OK, HTTPStatus.PRECONDITION_FAILED,
    ]


@requires_db
@pytest.mark.asyncio
async def test_if_none_match_never_adopts_an_orphan_graph(
    api: httpx.AsyncClient, db_pool: asyncpg.Pool,
) -> None:
    # A graph unregistered the old way: no registration kept. An admin may adopt
    # it by registering its name, but not under If-None-Match: *.
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM graphos_meta.tenants WHERE name = $1;", NAME)
    forget(NAME)
    star = {**_as("admin"), "If-None-Match": "*"}
    for query in ("?dry_run=true", ""):
        resp = await api.post(f"/v1/tenants{query}", json={"name": NAME}, headers=star)
        assert resp.status_code == HTTPStatus.PRECONDITION_FAILED, resp.text
        assert "would adopt it" in resp.json()["detail"]
    assert NAME not in registered_tenants()


@requires_db
@pytest.mark.asyncio
async def test_a_rename_dry_run_answers_the_version_it_planned_on(
    api: httpx.AsyncClient,
) -> None:
    read = await api.get(f"/v1/{NAME}/schema", headers=_as("owner"))
    tag = read.headers["ETag"]
    rename = {"kind": "label", "from": "Doc", "to": "Document"}
    plan = await api.post(f"/v1/tenants/{NAME}/schema:rename?dry_run=true", json=rename,
                          headers={**_as("owner"), "If-Match": tag})
    assert plan.status_code == HTTPStatus.OK, plan.text
    assert plan.headers["ETag"] == tag
    stale = await api.post(f"/v1/tenants/{NAME}/schema:rename?dry_run=true", json=rename,
                           headers={**_as("owner"), "If-Match": '"stale"'})
    assert stale.status_code == HTTPStatus.PRECONDITION_FAILED, stale.text

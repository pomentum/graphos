"""DB tests: a tenant unregistered, restored and deleted (``graphos.lifecycle``),
and the web UI preferences (``graphos.preferences``).

Unregistering keeps the data and the registration, so restoring brings the
tenant back as it was; registering the name again restores it for its owner
and is refused to anyone else; deleting with its data removes the graph and
graphos's rows about it, and frees the name.
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

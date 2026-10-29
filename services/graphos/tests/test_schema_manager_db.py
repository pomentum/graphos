"""DB tests: a tenant's schema manager and the drift report (ADR-0013).

A program (``loader``, a service account owning the tenant) registers the
schema and manages it; people (``olive``, also an owner, and ``max``, a
maintainer) may change its styles but not its structure, by any write path;
the program's registrations keep people's styles; an owner releases it. Drops
and pair removals are recorded and reported when a registration declares them
again, which creates them again (nothing refused).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import asyncpg
import httpx
import pytest_asyncio

from graphos.app import create_app
from graphos.auth import StaticAuthenticator
from graphos.bootstrap import forget
from graphos.config import Settings
from graphos.identity import Principal

from .conftest import requires_db

NAME = "graphos_test_manager"
SCHEMA: dict[str, Any] = {
    "name": NAME,
    "vertex_labels": ["Doc", "Team", "Old"],
    "edge_labels": ["OWNS", "LINKS_TO"],
    "edge_endpoints": {"OWNS": [{"src": "Team", "dst": "Doc"}]},
    "display": {"vertex": {"Doc": {"color": "#336699"}}},
    "deprecated": {"vertex": {"Old": {"deprecated": True}}},
    "meta": {"backend": "loader"},
}
PRINCIPALS = {
    "loader": Principal.static("loader", tenants={NAME: "owner"},
                               create_tenants=["graphos_test_*"]),
    "olive": Principal.static("olive", tenants={NAME: "owner"}),
    "max": Principal.static("max", tenants={NAME: "maintain"}),
}


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _fresh(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1;", NAME):
            await conn.execute("SELECT drop_graph($1::name, true);", NAME)
        for table, column in (
            ("tenants", "name"), ("unregistered_tenants", "name"), ("edge_endpoints", "tenant"),
            ("tenant_settings", "tenant"), ("changes", "tenant"), ("change_horizon", "tenant"),
            ("jobs", "tenant"), ("saved_queries", "tenant"), ("node_properties", "tenant"),
            ("schema_removals", "tenant"),
        ):
            await conn.execute(f"DELETE FROM graphos_meta.{table} WHERE {column} = $1;", NAME)
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
        yield client
    await _fresh(db_pool)


async def _register(api: httpx.AsyncClient, token: str, body: dict[str, Any],
                    *, dry_run: bool = False) -> httpx.Response:
    return await api.post("/v1/tenants" + ("?dry_run=true" if dry_run else ""),
                          json={"name": NAME, **body}, headers=_as(token))


async def _schema(api: httpx.AsyncClient) -> tuple[dict[str, Any], str]:
    read = await api.get(f"/v1/{NAME}/schema", headers=_as("olive"))
    assert read.status_code == HTTPStatus.OK, read.text
    return read.json(), read.headers["ETag"]


def _managed(answer: httpx.Response) -> None:
    assert answer.status_code == HTTPStatus.CONFLICT, answer.text
    body = answer.json()
    assert body["code"] == "schema_managed" and body["schema_manager"] == "user:loader"
    assert isinstance(body["detail"], str) and "managed by user:loader" in body["detail"]


@requires_db
async def test_a_program_registers_and_manages_its_schema(api: httpx.AsyncClient) -> None:
    created = await _register(api, "loader", {**SCHEMA, "schema_manager": "user:loader"})
    assert created.status_code == HTTPStatus.OK, created.text
    schema, _ = await _schema(api)
    assert schema["schema_manager"] == "user:loader"
    info = await api.get(f"/v1/tenants/{NAME}", headers=_as("olive"))
    assert info.json()["schema_manager"] == "user:loader"


@requires_db
async def test_others_may_change_styles_but_not_the_structure(api: httpx.AsyncClient) -> None:
    await _register(api, "loader", {**SCHEMA, "schema_manager": "user:loader"})
    before, _ = await _schema(api)

    styled = await _register(api, "max", {"display": {"vertex": {"Team": {"color": "#AA0000"}}}})
    assert styled.status_code == HTTPStatus.OK, styled.text
    # Declaring again what is stored is not a change.
    same = await _register(api, "olive", {"vertex_labels": ["Doc"]})
    assert same.status_code == HTTPStatus.OK, same.text

    for token, body in [
        ("olive", {"vertex_labels": ["Extra"]}),
        ("olive", {"edge_endpoints": {"OWNS": [{"src": "Team", "dst": "Team"}]}}),
        ("max", {"property_schema": {"vertex": {"Doc": {
            "properties": {"t": {"type": "string"}}}}}}),
        ("max", {"deprecated": {"vertex": {"Team": {"deprecated": True}}}}),
        ("max", {"meta": {"backend": "someone else"}}),
        ("max", {"strict_endpoints": True}),
        ("max", {"community_algorithm": "leiden"}),
    ]:
        _managed(await _register(api, token, body))
        _managed(await _register(api, token, body, dry_run=True))
    _managed(await api.post("/v1/tenants?force_shrink=true", headers=_as("olive"),
                            json={"name": NAME, "vertex_labels": ["Doc"]}))
    _managed(await api.post(f"/v1/tenants/{NAME}/schema:rename", headers=_as("olive"),
                            json={"kind": "label", "from": "Team", "to": "Group"}))
    _managed(await api.delete(f"/v1/tenants/{NAME}/labels/Old", headers=_as("olive")))
    _managed(await api.delete(f"/v1/tenants/{NAME}/edge_endpoints", headers=_as("olive"),
                              params={"type": "OWNS", "src": "Team", "dst": "Doc"}))

    after, _ = await _schema(api)
    # A merge stores the labels in an order of its own: compare them as sets.
    assert set(after["vertex_labels"]) == set(before["vertex_labels"])
    assert after["edge_endpoints"] == before["edge_endpoints"]
    assert after["display"]["vertex"]["Team"]["color"] == "#AA0000"


@requires_db
async def test_the_managers_registration_keeps_peoples_styles(api: httpx.AsyncClient) -> None:
    await _register(api, "loader", {**SCHEMA, "schema_manager": "user:loader"})
    await _register(api, "olive", {"display": {"vertex": {"Doc": {"color": "#00AA00"}}}})
    # The program starts again with its own styles, one of them new.
    again = await _register(api, "loader", {**SCHEMA, "display": {"vertex": {
        "Doc": {"color": "#336699"}, "Team": {"color": "#123456"}}}})
    assert again.status_code == HTTPStatus.OK, again.text
    schema, _ = await _schema(api)
    assert schema["display"]["vertex"]["Doc"]["color"] == "#00AA00"   # people's, kept
    assert schema["display"]["vertex"]["Team"]["color"] == "#123456"  # the program's, added
    # And the manager changes the structure as it likes.
    grown = await _register(api, "loader", {"vertex_labels": ["Extra"]})
    assert grown.status_code == HTTPStatus.OK, grown.text


@requires_db
async def test_only_an_owner_sets_or_releases_the_manager(api: httpx.AsyncClient) -> None:
    created = await _register(api, "loader", SCHEMA)
    assert created.status_code == HTTPStatus.OK, created.text
    # The first merge stores the spec in its merged form (label order); read
    # the version after one, so only the manager can change it below.
    await _register(api, "olive", {"vertex_labels": ["Doc"]})
    _, unmanaged = await _schema(api)
    denied = await _register(api, "max", {"schema_manager": "user:max"})
    assert denied.status_code == HTTPStatus.FORBIDDEN
    bad = await _register(api, "olive", {"schema_manager": "loader"})
    assert bad.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    managed = await _register(api, "olive", {"schema_manager": "user:loader"})
    assert managed.status_code == HTTPStatus.OK, managed.text
    schema, tag = await _schema(api)
    assert schema["schema_manager"] == "user:loader" and tag != unmanaged

    released = await _register(api, "olive", {"schema_manager": None})
    assert released.status_code == HTTPStatus.OK, released.text
    schema, tag = await _schema(api)
    assert schema["schema_manager"] is None and tag != unmanaged
    # Set and released again: the same version as after the first release
    # (the manager is in the version only while set).
    await _register(api, "olive", {"schema_manager": "user:loader"})
    await _register(api, "olive", {"schema_manager": None})
    _, again = await _schema(api)
    assert again == tag
    grown = await _register(api, "olive", {"vertex_labels": ["Extra"]})
    assert grown.status_code == HTTPStatus.OK, grown.text


@requires_db
async def test_a_drop_is_reported_and_recreated_not_refused(api: httpx.AsyncClient) -> None:
    await _register(api, "loader", SCHEMA)   # unmanaged
    dropped = await api.delete(f"/v1/tenants/{NAME}/labels/Old", headers=_as("olive"))
    assert dropped.status_code == HTTPStatus.OK, dropped.text
    pair = await api.delete(f"/v1/tenants/{NAME}/edge_endpoints", headers=_as("olive"),
                            params={"type": "OWNS", "src": "Team", "dst": "Doc"})
    assert pair.status_code == HTTPStatus.OK, pair.text

    preview = await _register(api, "loader", SCHEMA, dry_run=True)
    assert preview.status_code == HTTPStatus.OK, preview.text
    recreates = preview.json()["drift"]["recreates"]
    assert {(r["kind"], r["name"], r["removed_by"]) for r in recreates} == {
        ("vertex_label", "Old", "olive"), ("edge_endpoint", "OWNS", "olive")}

    again = await _register(api, "loader", SCHEMA)
    assert again.status_code == HTTPStatus.OK, again.text
    schema, _ = await _schema(api)
    assert "Old" in schema["vertex_labels"]
    assert {"src": "Team", "dst": "Doc"} in schema["edge_endpoints"]["OWNS"]
    # Registered again: no longer a removal.
    later = await _register(api, "loader", SCHEMA, dry_run=True)
    assert "recreates" not in (later.json()["drift"] or {})


@requires_db
async def test_the_dry_run_reports_what_people_changed(api: httpx.AsyncClient) -> None:
    await _register(api, "loader", SCHEMA)
    await _register(api, "olive", {"vertex_labels": ["Added"],
                                   "display": {"vertex": {"Doc": {"color": "#00AA00"}}}})
    preview = await _register(api, "loader", SCHEMA, dry_run=True)
    assert preview.status_code == HTTPStatus.OK, preview.text
    drift = preview.json()["drift"]
    assert drift["not_declared"]["vertex_labels"] == ["Added"]
    assert drift["overwrites"]["display"] == {"vertex": ["Doc"]}
    new = await api.post("/v1/tenants?dry_run=true", headers=_as("loader"),
                         json={"name": "graphos_test_manager_new", "vertex_labels": ["X"]})
    assert new.status_code == HTTPStatus.OK and new.json()["drift"] is None

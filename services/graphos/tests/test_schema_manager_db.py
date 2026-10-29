"""DB tests: a tenant's schema manager and the drift report (ADR-0013, ADR-0014).

A program (``loader``, a service account owning the tenant) registers the
schema and manages it; people (``olive``, also an owner, and ``max``, a
maintainer) may change its styles and its community algorithm but not its
structure, by any write path. The program's registrations keep people's
styles unless they say ``display_merge: entries``; a replace keeps them too.
An owner sets or releases the manager; a manager that is not an owner names
itself at every start. Drops, pair removals and what a replace removes are
recorded and reported when declared again, which creates them again.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http import HTTPStatus
from types import SimpleNamespace
from typing import Any

import asyncpg
import httpx
import pytest_asyncio

from graphos.app import create_app
from graphos.auth import StaticAuthenticator
from graphos.bootstrap import forget
from graphos.config import Settings
from graphos.identity import Principal
from graphos.oidc import Identity

from .conftest import requires_db

NAME = "graphos_test_manager"
SCHEMA: dict[str, Any] = {
    "name": NAME,
    "vertex_labels": ["Doc", "Team", "Old"],
    "edge_labels": ["OWNS", "LINKS_TO"],
    "edge_endpoints": {"OWNS": [{"src": "Team", "dst": "Doc"}]},
    "search_indexes": {"titles": {"labels": ["Doc"], "properties": ["title"]}},
    "display": {"vertex": {"Doc": {"color": "#336699"}}},
    "deprecated": {"vertex": {"Old": {"deprecated": True}}},
    "meta": {"backend": "loader"},
}
PRINCIPALS = {
    "loader": Principal.static("loader", tenants={NAME: "owner"},
                               create_tenants=["graphos_test_*"]),
    "olive": Principal.static("olive", tenants={NAME: "owner"}),
    "max": Principal.static("max", tenants={NAME: "maintain"}),
    "bot": Principal.static("bot", tenants={NAME: "maintain"}),
    # As production builds it (``AccessStore.principal_for``): ``oidc-sub:<sub>``.
    "sso": Principal(consumer="Ada",
                     subject=Identity(subject="ada-sub-1", consumer="Ada").subjects()[0],
                     levels=Principal.static("x", tenants={NAME: "maintain"}).levels),
}


class _Accounts:
    """The account store at its port: who exists and who is disabled."""

    def __init__(self) -> None:
        enabled = SimpleNamespace(disabled=False)
        self._users = {"loader": enabled, "olive": enabled, "max": enabled, "bot": enabled,
                       "gone": SimpleNamespace(disabled=True)}

    def user(self, name: str) -> Any:
        return self._users.get(name)

    async def grant(self, *_args: Any, **_kwargs: Any) -> bool:
        return True


def _as(token: str, etag: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if etag is not None:
        headers["If-Match"] = etag
    return headers


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
    app.state.access_store = _Accounts()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        yield client
    await _fresh(db_pool)


async def _register(api: httpx.AsyncClient, token: str, body: dict[str, Any], *,
                    dry_run: bool = False, replace: bool = False) -> httpx.Response:
    query = "&".join(q for q, on in (("dry_run=true", dry_run), ("force_shrink=true", replace))
                     if on)
    return await api.post("/v1/tenants" + (f"?{query}" if query else ""),
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


async def _managed_tenant(api: httpx.AsyncClient) -> None:
    created = await _register(api, "loader", {**SCHEMA, "schema_manager": "self"})
    assert created.status_code == HTTPStatus.OK, created.text
    assert created.json()["schema_manager"] == "user:loader"   # echoed (T10)


@requires_db
async def test_a_program_registers_and_manages_its_schema(api: httpx.AsyncClient) -> None:
    await _managed_tenant(api)
    schema, _ = await _schema(api)
    assert schema["schema_manager"] == "user:loader" and schema["schema_manager_since"]
    info = await api.get(f"/v1/tenants/{NAME}", headers=_as("olive"))
    assert info.json()["schema_manager"] == "user:loader"


@requires_db
async def test_others_may_change_styles_but_not_the_structure(api: httpx.AsyncClient) -> None:
    await _managed_tenant(api)
    before, _ = await _schema(api)

    styled = await _register(api, "max", {"display": {"vertex": {"Team": {"color": "#AA0000"}}}})
    assert styled.status_code == HTTPStatus.OK, styled.text
    same = await _register(api, "olive", {"vertex_labels": ["Doc"]})   # stored: no change
    assert same.status_code == HTTPStatus.OK, same.text
    algorithm = await _register(api, "max", {"community_algorithm": "leiden"})   # T7
    assert algorithm.status_code == HTTPStatus.OK, algorithm.text

    for token, body in [
        ("olive", {"vertex_labels": ["Extra"]}),
        ("olive", {"edge_endpoints": {"OWNS": [{"src": "Team", "dst": "Team"}]}}),
        ("max", {"property_schema": {"vertex": {"Doc": {
            "properties": {"t": {"type": "string"}}}}}}),
        ("max", {"deprecated": {"vertex": {"Team": {"deprecated": True}}}}),
        ("max", {"meta": {"backend": "someone else"}}),
        ("max", {"strict_endpoints": True}),
        ("max", {"search_indexes": {"titles": {"labels": ["Doc"], "properties": ["name"]}}}),  # T2
    ]:
        _managed(await _register(api, token, body))
        _managed(await _register(api, token, body, dry_run=True))
    _managed(await _register(api, "olive", {"vertex_labels": ["Doc"]}, replace=True))
    _managed(await api.post(f"/v1/tenants/{NAME}/schema:rename", headers=_as("olive"),
                            json={"kind": "label", "from": "Team", "to": "Group"}))
    _managed(await api.delete(f"/v1/tenants/{NAME}/labels/Old", headers=_as("olive")))
    _managed(await api.delete(f"/v1/tenants/{NAME}/edge_endpoints", headers=_as("olive"),
                              params={"type": "OWNS", "src": "Team", "dst": "Doc"}))

    after, _ = await _schema(api)
    # A merge stores the labels in an order of its own: compare them as sets.
    assert set(after["vertex_labels"]) == set(before["vertex_labels"])
    assert after["edge_endpoints"] == before["edge_endpoints"]
    assert after["search_indexes"] == before["search_indexes"]
    assert after["display"]["vertex"]["Team"]["color"] == "#AA0000"
    assert after["community_algorithm"] == "leiden"


@requires_db
async def test_the_managers_registration_keeps_peoples_styles(api: httpx.AsyncClient) -> None:
    await _managed_tenant(api)
    await _register(api, "olive", {"display": {"vertex": {"Doc": {"color": "#00AA00"}}}})
    again = await _register(api, "loader", {**SCHEMA, "display": {"vertex": {
        "Doc": {"color": "#336699"}, "Team": {"color": "#123456"}}}})
    assert again.status_code == HTTPStatus.OK, again.text
    schema, _ = await _schema(api)
    assert schema["display"]["vertex"]["Doc"]["color"] == "#00AA00"   # people's, kept
    assert schema["display"]["vertex"]["Team"]["color"] == "#123456"  # the program's, added
    grown = await _register(api, "loader", {"vertex_labels": ["Extra"]})
    assert grown.status_code == HTTPStatus.OK, grown.text


@requires_db
async def test_a_managers_replace_keeps_every_style(api: httpx.AsyncClient) -> None:   # T1
    await _managed_tenant(api)
    await _register(api, "olive", {"display": {"vertex": {"Team": {"color": "#00AA00"}}}})
    replaced = await _register(api, "loader", {**SCHEMA, "display": {}}, replace=True)
    assert replaced.status_code == HTTPStatus.OK, replaced.text
    schema, _ = await _schema(api)
    assert schema["display"]["vertex"]["Doc"]["color"] == "#336699"
    assert schema["display"]["vertex"]["Team"]["color"] == "#00AA00"


@requires_db
async def test_a_managers_replace_drops_the_styles_of_labels_it_removes(
    api: httpx.AsyncClient,
) -> None:   # T1, second review
    await _managed_tenant(api)
    await _register(api, "olive", {"display": {"vertex": {"Team": {"color": "#00AA00"}}}})
    smaller = {**SCHEMA, "vertex_labels": ["Doc", "Old"], "edge_endpoints": {}, "display": {}}
    preview = await _register(api, "loader", smaller, dry_run=True, replace=True)
    assert preview.status_code == HTTPStatus.OK, preview.text
    replaced = await _register(api, "loader", smaller, replace=True)
    assert replaced.status_code == HTTPStatus.OK, replaced.text
    schema, _ = await _schema(api)
    assert schema["display"]["vertex"] == {"Doc": {"color": "#336699"}}
    # The stored styles name only labels the tenant has: they register again as they are.
    again = await _register(api, "loader", {"display": schema["display"],
                                            "display_merge": "entries"}, dry_run=True)
    assert again.status_code == HTTPStatus.OK, again.text


@requires_db
async def test_claiming_the_schema_keeps_peoples_styles_from_the_first_call(
    api: httpx.AsyncClient,
) -> None:   # third review
    await _register(api, "loader", SCHEMA)   # unmanaged
    await _register(api, "olive", {"display": {"vertex": {"Team": {"color": "#00AA00"}}}})
    claimed = await _register(api, "loader", {
        **SCHEMA, "display": {"vertex": {"Team": {"color": "#FF0000"}}}, "schema_manager": "self",
    })
    assert claimed.status_code == HTTPStatus.OK, claimed.text
    schema, _ = await _schema(api)
    assert schema["schema_manager"] == "user:loader"
    assert schema["display"]["vertex"]["Team"]["color"] == "#00AA00"


@requires_db
async def test_the_manager_may_correct_its_styles_when_it_says_so(
    api: httpx.AsyncClient,
) -> None:   # T1
    await _managed_tenant(api)
    await _register(api, "olive", {"display": {"vertex": {"Team": {"color": "#00AA00"}}}})
    corrected = await _register(api, "loader", {
        "display": {"vertex": {"Doc": {"color": "#000000"}, "Team": None}},
        "display_merge": "entries",
    })
    assert corrected.status_code == HTTPStatus.OK, corrected.text
    schema, _ = await _schema(api)
    assert schema["display"]["vertex"] == {"Doc": {"color": "#000000"}}


@requires_db
async def test_owners_set_the_manager_and_a_manager_names_itself_freely(
    api: httpx.AsyncClient,
) -> None:   # T3, T8
    await _register(api, "loader", SCHEMA)
    await _register(api, "olive", {"vertex_labels": ["Doc"]})   # the stored spec's merged form
    _, unmanaged = await _schema(api)
    assert (await _register(api, "max", {"schema_manager": "user:max"})).status_code == 403
    for bad in ("loader", "user:nobody", "user:gone"):
        answer = await _register(api, "olive", {"schema_manager": bad})
        assert answer.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, (bad, answer.text)

    named = await _register(api, "olive", {"schema_manager": "user:bot"})
    assert named.status_code == HTTPStatus.OK, named.text
    # The manager, only a maintainer, names itself at every start: no change, no refusal.
    for value in ("user:bot", "self"):
        start = await _register(api, "bot", {**SCHEMA, "schema_manager": value})
        assert start.status_code == HTTPStatus.OK, start.text
    schema, tag = await _schema(api)
    assert schema["schema_manager"] == "user:bot" and tag != unmanaged

    released = await _register(api, "olive", {"schema_manager": None})
    assert released.status_code == HTTPStatus.OK and released.json()["schema_manager"] is None
    _, after_release = await _schema(api)
    await _register(api, "olive", {"schema_manager": "user:bot"})
    await _register(api, "olive", {"schema_manager": None})
    _, again = await _schema(api)
    assert again == after_release   # in the version only while set


@requires_db
async def test_a_single_sign_on_caller_can_manage(api: httpx.AsyncClient) -> None:   # T8
    await _register(api, "loader", SCHEMA)
    named = await _register(api, "olive", {"schema_manager": "oidc-sub:ada-sub-1"})
    assert named.status_code == HTTPStatus.OK, named.text
    grown = await _register(api, "sso", {"vertex_labels": ["Extra"]})
    assert grown.status_code == HTTPStatus.OK, grown.text
    refused = await _register(api, "max", {"vertex_labels": ["More"]})
    assert refused.status_code == HTTPStatus.CONFLICT


@requires_db
async def test_no_manager_with_authentication_off(db_pool: asyncpg.Pool) -> None:   # T8
    await _fresh(db_pool)
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = db_pool
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        answer = await client.post("/v1/tenants", json={**SCHEMA, "schema_manager": "self"})
        assert answer.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, answer.text
    await _fresh(db_pool)


@requires_db
async def test_unregister_and_restore_keep_the_manager(api: httpx.AsyncClient) -> None:   # T6
    await _managed_tenant(api)
    since = (await _schema(api))[0]["schema_manager_since"]
    gone = await api.delete(f"/v1/tenants/{NAME}", headers=_as("olive"))
    assert gone.status_code == HTTPStatus.OK, gone.text
    # A registration that would restore it is judged as the managed tenant it
    # restores, preview or not, and a refused one restores nothing.
    more = {"vertex_labels": ["Doc", "Team", "Old", "Extra"]}
    _managed(await _register(api, "olive", more, dry_run=True))
    _managed(await _register(api, "olive", more))
    still_gone = await api.get(f"/v1/{NAME}/schema", headers=_as("olive"))
    assert still_gone.status_code == HTTPStatus.NOT_FOUND, still_gone.text
    back = await api.post(f"/v1/tenants/{NAME}/restore", headers=_as("olive"))
    assert back.status_code == HTTPStatus.OK, back.text
    schema, _ = await _schema(api)
    assert schema["schema_manager"] == "user:loader"
    assert schema["schema_manager_since"] == since


@requires_db
async def test_drops_and_replaces_are_reported_and_recreated(api: httpx.AsyncClient) -> None:
    await _register(api, "loader", SCHEMA)   # unmanaged
    assert (await api.delete(f"/v1/tenants/{NAME}/labels/Old",
                             headers=_as("olive"))).status_code == HTTPStatus.OK
    assert (await api.delete(f"/v1/tenants/{NAME}/edge_endpoints", headers=_as("olive"), params={
        "type": "OWNS", "src": "Team", "dst": "Doc"})).status_code == HTTPStatus.OK
    preview = await _register(api, "loader", SCHEMA, dry_run=True)
    recreates = preview.json()["drift"]["recreates"]
    assert [r["name"] for r in recreates["vertex_labels"]] == ["Old"]
    assert recreates["edge_endpoints"][0]["pair"] == ["OWNS", "Team", "Doc"]
    assert recreates["vertex_labels"][0]["removed_by"] == "olive"

    again = await _register(api, "loader", SCHEMA)
    assert again.status_code == HTTPStatus.OK, again.text
    later = await _register(api, "loader", SCHEMA, dry_run=True)
    assert (later.json()["drift"] or {}).get("recreates") is None

    # What a replace removes is a removal too (T14).
    shrunk = await _register(api, "loader", {**SCHEMA, "vertex_labels": ["Doc", "Team"],
                                             "deprecated": {}}, replace=True)
    assert shrunk.status_code == HTTPStatus.OK, shrunk.text
    preview = await _register(api, "loader", SCHEMA, dry_run=True)
    assert [r["name"] for r in preview.json()["drift"]["recreates"]["vertex_labels"]] == ["Old"]


@requires_db
async def test_the_dry_run_reports_and_names_its_version(api: httpx.AsyncClient) -> None:
    await _managed_tenant(api)
    await _register(api, "olive", {"display": {"vertex": {"Doc": {"color": "#00AA00"}}}})
    await _register(api, "max", {"display": {"vertex": {"Team": {"color": "#AA0000"}}}})
    preview = await _register(api, "loader", SCHEMA, dry_run=True)
    assert preview.status_code == HTTPStatus.OK, preview.text
    drift = preview.json()["drift"]
    # The manager declares Doc's style: not "not declared"; Team's is people's.
    assert (drift["not_declared"] or {}).get("display") == {"vertex": ["Team"]}
    assert drift["overwrites"] is None   # missing_only overwrites nothing
    _, tag = await _schema(api)
    assert preview.headers["ETag"] == tag   # T9
    applied = await api.post("/v1/tenants", json=SCHEMA, headers=_as("loader", tag))
    assert applied.status_code == HTTPStatus.OK, applied.text

    replace_preview = await _register(api, "loader", {**SCHEMA, "vertex_labels": ["Doc", "Team"],
                                                      "deprecated": {}},
                                      dry_run=True, replace=True)
    assert replace_preview.json()["drift"]["removes"]["vertex_labels"] == ["Old"]
    new = await api.post("/v1/tenants?dry_run=true", headers=_as("loader"),
                         json={"name": "graphos_test_manager_new", "vertex_labels": ["X"]})
    assert new.status_code == HTTPStatus.OK and new.json()["drift"] is None

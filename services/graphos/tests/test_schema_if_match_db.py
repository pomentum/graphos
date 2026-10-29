"""DB tests: a tenant's schema version as an ETag, and schema writes on If-Match.

Two people edit one tenant's schema from the version they loaded: the first
save wins, the second is refused (412, with the current ETag) instead of
silently undoing the first. Every schema write takes If-Match: registration
(and its dry run), renames, label drops, pair removals; without it they work
as before. Two writes from the same version at once: exactly one wins.
"""

from __future__ import annotations

import asyncio
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

NAME = "graphos_test_ifmatch"
SCHEMA: dict[str, Any] = {
    "name": NAME,
    "vertex_labels": ["Doc", "Team", "Old"],
    "edge_labels": ["LINKS_TO", "OWNS"],
    "edge_endpoints": {"OWNS": [{"src": "Team", "dst": "Doc"}]},
    "display": {"vertex": {"Doc": {"color": "#336699"}}},
    "deprecated": {"vertex": {"Old": {"deprecated": True}}},
}
PRINCIPALS = {
    "owner": Principal.static("olive", tenants={NAME: "owner"}),
    "reader": Principal.static("rita", tenants={NAME: "read"}),
    "creator": Principal.static("svc", create_tenants=["graphos_test_*"]),
}


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
        registered = await client.post("/v1/tenants", json=SCHEMA, headers=_as("creator"))
        assert registered.status_code == HTTPStatus.OK, registered.text
        yield client
    await _fresh(db_pool)


async def _schema(api: httpx.AsyncClient) -> tuple[dict[str, Any], str]:
    read = await api.get(f"/v1/{NAME}/schema", headers=_as("reader"))
    assert read.status_code == HTTPStatus.OK, read.text
    return read.json(), read.headers["ETag"]


async def _style(api: httpx.AsyncClient, color: str, etag: str | None) -> httpx.Response:
    return await api.post(
        "/v1/tenants", headers=_as("owner", etag),
        json={"name": NAME, "display": {"vertex": {"Doc": {"color": color}}}},
    )


@requires_db
async def test_every_schema_read_gives_the_same_etag(api: httpx.AsyncClient) -> None:
    _, etag = await _schema(api)
    assert etag.startswith('"') and etag.endswith('"')
    tenant = await api.get(f"/v1/tenants/{NAME}", headers=_as("owner"))
    exported = await api.get(f"/v1/tenants/{NAME}/spec", headers=_as("owner"))
    assert tenant.headers["ETag"] == etag
    assert exported.headers["ETag"] == etag


@requires_db
async def test_the_second_of_two_edits_from_one_version_is_refused(api: httpx.AsyncClient) -> None:
    _, loaded = await _schema(api)          # two people load the same schema
    first = await _style(api, "#AA0000", loaded)
    assert first.status_code == HTTPStatus.OK, first.text
    second = await _style(api, "#00AA00", loaded)
    assert second.status_code == HTTPStatus.PRECONDITION_FAILED, second.text
    body, now = await _schema(api)
    # The first edit stands, and the refusal said which version is current.
    assert body["display"]["vertex"]["Doc"]["color"] == "#AA0000"
    assert second.headers["ETag"] == now == first.headers["ETag"]
    assert "changed since it was read" in second.json()["detail"]
    # Read again, the second person's edit goes through.
    again = await _style(api, "#00AA00", now)
    assert again.status_code == HTTPStatus.OK, again.text


@requires_db
async def test_two_writes_from_one_version_at_once_one_wins(api: httpx.AsyncClient) -> None:
    _, loaded = await _schema(api)
    answers = await asyncio.gather(*(_style(api, f"#0000{i:02x}", loaded) for i in range(4)))
    codes = sorted(a.status_code for a in answers)
    assert codes == [HTTPStatus.OK] + [HTTPStatus.PRECONDITION_FAILED] * 3, codes


@requires_db
async def test_without_if_match_writes_work_as_before(api: httpx.AsyncClient) -> None:
    _, before = await _schema(api)
    plain = await _style(api, "#123456", None)
    assert plain.status_code == HTTPStatus.OK, plain.text
    _, after = await _schema(api)
    assert after != before and plain.headers["ETag"] == after
    # A program's full registration, as on every start: unchanged schema, same version.
    again = await api.post("/v1/tenants", json={**SCHEMA, "display": {
        "vertex": {"Doc": {"color": "#123456"}}}}, headers=_as("owner"))
    assert again.status_code == HTTPStatus.OK
    assert again.headers["ETag"] == after


@requires_db
async def test_any_version_and_a_new_tenant(api: httpx.AsyncClient) -> None:
    star = await _style(api, "#ABCDEF", "*")
    assert star.status_code == HTTPStatus.OK, star.text
    new = await api.post(
        "/v1/tenants", headers=_as("creator", '"0"'),
        json={"name": "graphos_test_ifmatch_new", "vertex_labels": ["X"]},
    )
    assert new.status_code == HTTPStatus.PRECONDITION_FAILED
    assert "not registered" in new.json()["detail"]


@requires_db
async def test_a_stale_dry_run_is_refused_too(api: httpx.AsyncClient) -> None:
    _, loaded = await _schema(api)
    assert (await _style(api, "#AA0000", loaded)).status_code == HTTPStatus.OK
    dry = await api.post(
        "/v1/tenants?dry_run=true", headers=_as("owner", loaded),
        json={"name": NAME, "vertex_labels": ["Extra"]},
    )
    assert dry.status_code == HTTPStatus.PRECONDITION_FAILED


@requires_db
async def test_renames_drops_and_pair_removals_take_if_match(api: httpx.AsyncClient) -> None:
    _, stale = await _schema(api)
    assert (await _style(api, "#AA0000", stale)).status_code == HTTPStatus.OK
    body, now = await _schema(api)

    rename = {"kind": "label", "from": "Team", "to": "Group"}
    refused = [
        await api.post(f"/v1/tenants/{NAME}/schema:rename", json=rename,
                       headers=_as("owner", stale)),
        await api.post(f"/v1/tenants/{NAME}/schema:rename?dry_run=true", json=rename,
                       headers=_as("owner", stale)),
        await api.delete(f"/v1/tenants/{NAME}/labels/Old", headers=_as("owner", stale)),
        await api.delete(f"/v1/tenants/{NAME}/edge_endpoints", headers=_as("owner", stale),
                         params={"type": "OWNS", "src": "Team", "dst": "Doc"}),
    ]
    assert [r.status_code for r in refused] == [HTTPStatus.PRECONDITION_FAILED] * 4
    unchanged, still = await _schema(api)
    assert unchanged == body and still == now

    pair = await api.delete(f"/v1/tenants/{NAME}/edge_endpoints", headers=_as("owner", now),
                            params={"type": "OWNS", "src": "Team", "dst": "Doc"})
    assert pair.status_code == HTTPStatus.OK, pair.text
    dropped = await api.delete(f"/v1/tenants/{NAME}/labels/Old",
                               headers=_as("owner", pair.headers["ETag"]))
    assert dropped.status_code == HTTPStatus.OK, dropped.text
    renamed = await api.post(f"/v1/tenants/{NAME}/schema:rename", json=rename,
                             headers=_as("owner", dropped.headers["ETag"]))
    assert renamed.status_code == HTTPStatus.OK, renamed.text
    final, tag = await _schema(api)
    assert "Group" in final["vertex_labels"] and "Old" not in final["vertex_labels"]
    assert renamed.headers["ETag"] == tag

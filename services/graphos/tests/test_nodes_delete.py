"""``POST /v1/{tenant}/graph/nodes:delete`` without a database.

The tenant registry and :func:`graphos.writes.delete_nodes_by_ref` are
replaced, so these tests check what the route validates and what it hands
to the write; ``test_nodes_delete_db.py`` checks the write itself.
"""

from __future__ import annotations

from collections.abc import Iterable
from http import HTTPStatus

import asyncpg
import pytest
from fastapi.testclient import TestClient

from graphos import writes
from graphos.api import graph as graph_api
from graphos.app import create_app
from graphos.bootstrap import TenantSpec
from graphos.config import Settings
from graphos.cypher import CypherError

URL = "/v1/acme/graph/nodes:delete"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[list[tuple[str, str]]]:
    spec = TenantSpec(name="acme", vertex_labels=("Doc", "Tag"), edge_labels=("LINKS_TO",))
    monkeypatch.setattr(graph_api, "registered_tenants", lambda: {"acme": spec})
    seen: list[list[tuple[str, str]]] = []

    async def fake(_pool: object, *, tenant: str, refs: Iterable[tuple[str, str]]) -> tuple[
        int, int,
    ]:
        assert tenant == "acme"
        seen.append(list(refs))
        return 2, 5

    monkeypatch.setattr(writes, "delete_nodes_by_ref", fake)
    return seen


@pytest.fixture
def client() -> TestClient:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = object()
    return TestClient(app, raise_server_exceptions=False)


def test_the_route_deletes_every_ref_in_one_call(
    client: TestClient, calls: list[list[tuple[str, str]]],
) -> None:
    resp = client.post(URL, json={"nodes": [
        {"label": "Doc", "id": "a/b"}, {"label": "Tag", "id": "x"}, {"label": "Doc", "id": "a/b"},
    ]})
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    counts = ("nodes_requested", "nodes_deleted", "edges_deleted")
    assert (body["tenant"], *(body[k] for k in counts)) == ("acme", 3, 2, 5)
    assert calls == [[("Doc", "a/b"), ("Tag", "x"), ("Doc", "a/b")]]


@pytest.mark.parametrize(
    ("nodes", "status"),
    [
        ([], HTTPStatus.UNPROCESSABLE_ENTITY),
        ([{"label": "Doc", "id": f"n{i}"} for i in range(1001)], HTTPStatus.UNPROCESSABLE_ENTITY),
        ([{"label": "doc", "id": "a"}], HTTPStatus.UNPROCESSABLE_ENTITY),
        ([{"label": "Doc", "id": ""}], HTTPStatus.UNPROCESSABLE_ENTITY),
        ([{"label": "Doc"}], HTTPStatus.UNPROCESSABLE_ENTITY),
        ([{"label": "Doc", "id": "a"}, {"label": "Person", "id": "a"}], HTTPStatus.BAD_REQUEST),
    ],
)
def test_a_bad_batch_is_refused_before_any_write(
    client: TestClient, calls: list[list[tuple[str, str]]], nodes: list[dict], status: int,
) -> None:
    resp = client.post(URL, json={"nodes": nodes})
    assert resp.status_code == status, resp.text
    assert calls == []


def test_an_unregistered_tenant_is_404(
    client: TestClient, calls: list[list[tuple[str, str]]],
) -> None:
    resp = client.post("/v1/zeta/graph/nodes:delete", json={"nodes": [{"label": "Doc", "id": "a"}]})
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert calls == []


def test_a_database_error_is_400(
    client: TestClient, calls: list[list[tuple[str, str]]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing(*_args: object, **_kwargs: object) -> tuple[int, int]:
        raise CypherError("bad input", sqlstate="22P02", query="DELETE <label> by ids")

    monkeypatch.setattr(writes, "delete_nodes_by_ref", failing)
    resp = client.post(URL, json={"nodes": [{"label": "Doc", "id": "a"}]})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.json()["detail"].startswith("cypher error:")


def test_a_full_disk_is_507_without_the_databases_message(
    client: TestClient, calls: list[list[tuple[str, str]]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def full(*_args: object, **_kwargs: object) -> tuple[int, int]:
        raise CypherError(
            'could not extend file "base/16384/2619": No space left on device',
            sqlstate="53100", query="DELETE <label> by ids",
        )

    monkeypatch.setattr(writes, "delete_nodes_by_ref", full)
    resp = client.post(URL, json={"nodes": [{"label": "Doc", "id": "a"}]})
    assert resp.status_code == HTTPStatus.INSUFFICIENT_STORAGE
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert (resp.json()["status"], resp.json()["title"]) == (507, "Database out of disk space")
    assert "base/" not in resp.text


def test_a_lost_lock_conflict_is_503_with_retry_after(
    client: TestClient, calls: list[list[tuple[str, str]]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def deadlocked(*_args: object, **_kwargs: object) -> tuple[int, int]:
        raise CypherError("deadlock detected", sqlstate="40P01", query="DELETE <label> by ids")

    monkeypatch.setattr(writes, "delete_nodes_by_ref", deadlocked)
    resp = client.post(URL, json={"nodes": [{"label": "Doc", "id": "a"}]})
    assert resp.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert resp.headers["retry-after"] == "1"


@pytest.mark.asyncio
async def test_a_deadlock_is_run_again_then_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    async def losing_twice(_pool: object, _tenant: str, by_label: dict[str, set[str]]) -> tuple[
        int, int,
    ]:
        attempts.append(len(by_label))
        if len(attempts) < writes.DELETE_ATTEMPTS:
            raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")
        return 1, 0

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(writes, "_delete_by_label", losing_twice)
    monkeypatch.setattr(writes.asyncio, "sleep", no_wait)
    assert await writes.delete_nodes_by_ref(object(), tenant="acme", refs=[("Doc", "a")]) == (1, 0)
    assert len(attempts) == writes.DELETE_ATTEMPTS

    async def always_losing(*_args: object) -> tuple[int, int]:
        raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")

    monkeypatch.setattr(writes, "_delete_by_label", always_losing)
    with pytest.raises(CypherError) as caught:
        await writes.delete_nodes_by_ref(object(), tenant="acme", refs=[("Doc", "a")])
    assert caught.value.sqlstate == "40P01"

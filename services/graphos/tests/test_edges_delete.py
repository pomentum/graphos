"""``POST /v1/{tenant}/graph/edges:delete`` without a database.

The tenant registry and :func:`graphos.writes.delete_edges` are replaced, so
these tests check what the route validates, what it hands to the write and
how it answers the write's errors; ``test_edges_delete_db.py`` checks the
write itself.
"""

from __future__ import annotations

from collections.abc import Sequence
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

URL = "/v1/acme/graph/edges:delete"
ONE = {"edges": [{"type": "LINKS_TO", "src_id": "a", "dst_id": "b"}]}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[list[writes.EdgeKey]]:
    spec = TenantSpec(name="acme", vertex_labels=("Doc", "Tag"), edge_labels=("LINKS_TO", "TAGGED"))
    monkeypatch.setattr(graph_api, "registered_tenants", lambda: {"acme": spec})
    seen: list[list[writes.EdgeKey]] = []

    async def fake(_pool: object, *, tenant: str, edges: Sequence[writes.EdgeKey]) -> int:
        assert tenant == "acme"
        seen.append(list(edges))
        return 2

    monkeypatch.setattr(writes, "delete_edges", fake)
    return seen


@pytest.fixture
def client() -> TestClient:
    app = create_app(Settings(db_password="placeholder", auth_disabled=True))
    app.state.pool = object()
    return TestClient(app, raise_server_exceptions=False)


def test_the_route_deletes_every_edge_in_one_call(
    client: TestClient, calls: list[list[writes.EdgeKey]],
) -> None:
    resp = client.post(URL, json={"edges": [
        {"type": "LINKS_TO", "src_id": "a/b", "dst_id": "c"},
        {"type": "TAGGED", "src_id": "a/b", "dst_id": "x", "dst_label": "Tag"},
        {"type": "LINKS_TO", "src_id": "a/b", "dst_id": "c"},
    ]})
    assert resp.status_code == HTTPStatus.OK, resp.text
    body = resp.json()
    assert (body["tenant"], body["edges_requested"], body["edges_deleted"]) == ("acme", 3, 2)
    assert isinstance(body["elapsed_ms"], int)
    assert calls == [[
        ("LINKS_TO", "a/b", "c", None, None),
        ("TAGGED", "a/b", "x", None, "Tag"),
        ("LINKS_TO", "a/b", "c", None, None),
    ]]


def _edge(**fields: str) -> dict[str, str]:
    return {"type": "LINKS_TO", "src_id": "a", "dst_id": "b", **fields}


@pytest.mark.parametrize(
    ("edges", "status"),
    [
        ([], HTTPStatus.UNPROCESSABLE_ENTITY),
        ([_edge(src_id=f"n{i}") for i in range(1001)], HTTPStatus.UNPROCESSABLE_ENTITY),
        ([_edge(type="links to")], HTTPStatus.UNPROCESSABLE_ENTITY),
        ([_edge(src_id="")], HTTPStatus.UNPROCESSABLE_ENTITY),
        ([{"type": "LINKS_TO", "src_id": "a"}], HTTPStatus.UNPROCESSABLE_ENTITY),
        ([_edge(), _edge(type="CITES")], HTTPStatus.BAD_REQUEST),
        ([_edge(), _edge(src_label="Person")], HTTPStatus.BAD_REQUEST),
    ],
)
def test_a_bad_batch_is_refused_before_any_write(
    client: TestClient, calls: list[list[writes.EdgeKey]], edges: list[dict], status: int,
) -> None:
    resp = client.post(URL, json={"edges": edges})
    assert resp.status_code == status, resp.text
    assert calls == []


def test_an_unregistered_tenant_is_404(
    client: TestClient, calls: list[list[writes.EdgeKey]],
) -> None:
    resp = client.post("/v1/zeta/graph/edges:delete", json=ONE)
    assert resp.status_code == HTTPStatus.NOT_FOUND
    assert calls == []


def test_an_ambiguous_edge_is_409_naming_it(
    client: TestClient, calls: list[list[writes.EdgeKey]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ambiguous(*_args: object, **_kwargs: object) -> int:
        raise writes.AmbiguousEdgeError(("LINKS_TO", "a", "b"), ["Doc->Doc", "Doc->Tag"])

    monkeypatch.setattr(writes, "delete_edges", ambiguous)
    resp = client.post(URL, json=ONE)
    assert resp.status_code == HTTPStatus.CONFLICT
    detail = resp.json()["detail"]
    assert "LINKS_TO edges match from 'a' to 'b'" in detail
    assert "Doc->Tag" in detail and "Nothing was deleted" in detail


def test_a_database_error_is_400(
    client: TestClient, calls: list[list[writes.EdgeKey]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing(*_args: object, **_kwargs: object) -> int:
        raise CypherError("bad input", sqlstate="22P02", query="DELETE edges by their ends")

    monkeypatch.setattr(writes, "delete_edges", failing)
    resp = client.post(URL, json=ONE)
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert resp.json()["detail"].startswith("delete failed:")


def test_a_lost_lock_conflict_is_503_with_retry_after(
    client: TestClient, calls: list[list[writes.EdgeKey]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def deadlocked(*_args: object, **_kwargs: object) -> int:
        raise CypherError("deadlock detected", sqlstate="40P01", query="DELETE edges by their ends")

    monkeypatch.setattr(writes, "delete_edges", deadlocked)
    resp = client.post(URL, json=ONE)
    assert resp.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert resp.headers["retry-after"] == "1"


@pytest.mark.asyncio
async def test_a_deadlock_is_run_again_then_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []
    key: writes.EdgeKey = ("LINKS_TO", "a", "b", None, None)

    async def losing_twice(_pool: object, _tenant: str, edges: Sequence[writes.EdgeKey]) -> int:
        attempts.append(len(edges))
        if len(attempts) < writes.DELETE_ATTEMPTS:
            raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")
        return 1

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(writes, "_delete_edges_once", losing_twice)
    monkeypatch.setattr(writes.asyncio, "sleep", no_wait)
    assert await writes.delete_edges(object(), tenant="acme", edges=[key]) == 1
    assert len(attempts) == writes.DELETE_ATTEMPTS

    async def always_losing(*_args: object) -> int:
        raise asyncpg.exceptions.DeadlockDetectedError("deadlock detected")

    monkeypatch.setattr(writes, "_delete_edges_once", always_losing)
    with pytest.raises(CypherError) as caught:
        await writes.delete_edges(object(), tenant="acme", edges=[key])
    assert caught.value.sqlstate == "40P01"

"""The change feed over HTTP without a database.

The tables are the in-memory fake of ``_fake_feed.py`` and the tenant
registry is patched, so these tests check the routes and the Server-Sent
Events stream: cursors on the wire, paging, 400/404/410, the event framing,
keep-alives, waking and disconnects.  ``test_changes_stream.py`` runs the
stream against a live database.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from http import HTTPStatus
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from graphos import changes
from graphos.api import changes as changes_api
from graphos.api import graph as graph_api
from graphos.app import create_app
from graphos.auth import StaticAuthenticator
from graphos.bootstrap import TenantSpec
from graphos.config import Settings
from graphos.identity import Level, Principal

from ._fake_feed import FakeFeedDB, FakePool

T = "acme"
BASE = f"/v1/{T}/changes"


@pytest.fixture(autouse=True)
def _registered(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = TenantSpec(name=T, vertex_labels=("Doc", "Tag"), edge_labels=("LINKS_TO",))
    monkeypatch.setattr(graph_api, "registered_tenants", lambda: {T: spec})


@pytest.fixture
def db() -> FakeFeedDB:
    return FakeFeedDB()


@pytest.fixture
def pool(db: FakeFeedDB) -> FakePool:
    return FakePool(db)


@pytest.fixture
def client_no_lifespan(pool: FakePool) -> TestClient:
    app = create_app(Settings(
        db_password="placeholder", auth_disabled=True, changes_poll_s=0.01,
    ))
    app.state.pool = pool
    return TestClient(app, raise_server_exceptions=False)


def _sample(db: FakeFeedDB) -> None:
    db.add(T, 10, 0, ids=["a", "b"], count=2, consumer="ci", trace_id="t-1")
    db.add(T, 10, 1, op="patch", ids=["a"], rev=4)
    db.add(T, 11, 0, kind="edge", label="LINKS_TO", ids=[["a", "b"], ["b", "c"]], count=2,
           src_label="Doc", dst_label="Doc")
    db.add(T, 12, 0, op="schema", kind="tenant", label=None, ids=None,
           detail={"added": {"vertex_labels": ["Tag"]}})
    db.add(T, 13, 0, op="delete", ids=["c"], detail={"edges_removed": 1})


# ── GET …/changes ──────────────────────────────────────────────────────────


def test_following_next_pages_through_every_record_once(
    client_no_lifespan: TestClient, db: FakeFeedDB,
) -> None:
    _sample(db)
    db.add("other", 10, 5)
    seen: list[str] = []
    params: dict[str, Any] = {"limit": 2}
    for _ in range(10):
        body = client_no_lifespan.get(BASE, params=params).json()
        assert body["tenant"] == T
        seen += [c["cursor"] for c in body["changes"]]
        params["after"] = body["next"]
        if not body["more"]:
            break
    assert seen == ["10.0", "10.1", "11.0", "12.0", "13.0"]
    again = client_no_lifespan.get(BASE, params=params).json()
    assert (again["changes"], again["next"], again["more"]) == ([], "13.0", False)


def test_node_edge_and_schema_records_each_show_what_they_changed(
    client_no_lifespan: TestClient, db: FakeFeedDB,
) -> None:
    _sample(db)
    node, patch, edge, schema, delete = client_no_lifespan.get(BASE).json()["changes"]
    assert (node["ids"], node["edges"], node["count"], node["consumer"], node["trace_id"]) == (
        ["a", "b"], None, 2, "ci", "t-1",
    )
    assert (patch["op"], patch["rev"]) == ("patch", 4)
    assert (edge["ids"], edge["edges"], edge["src_label"]) == (
        None, [["a", "b"], ["b", "c"]], "Doc",
    )
    assert (schema["kind"], schema["ids"], schema["edges"], schema["detail"]) == (
        "tenant", None, None, {"added": {"vertex_labels": ["Tag"]}},
    )
    assert delete["detail"] == {"edges_removed": 1}


@pytest.mark.parametrize("after", ["abc", "1", "1.2.3", "-1.0", "1._2", "١"])
def test_a_malformed_cursor_is_400_before_any_database_work(
    client_no_lifespan: TestClient, pool: FakePool, after: str,
) -> None:
    resp = client_no_lifespan.get(BASE, params={"after": after})
    assert resp.status_code == HTTPStatus.BAD_REQUEST, resp.text
    assert "cursor" in resp.json()["detail"]
    assert pool.acquired == 0


def test_an_empty_cursor_means_from_the_oldest_record_kept(
    client_no_lifespan: TestClient, db: FakeFeedDB,
) -> None:
    _sample(db)
    resp = client_no_lifespan.get(BASE, params={"after": ""})
    assert resp.status_code == HTTPStatus.OK
    assert resp.json()["changes"][0]["cursor"] == "10.0"


@pytest.mark.parametrize("limit", [0, -1, 1001, "many"])
def test_a_limit_outside_one_to_a_thousand_is_refused(
    client_no_lifespan: TestClient, limit: object,
) -> None:
    resp = client_no_lifespan.get(BASE, params={"limit": limit})
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_a_pruned_cursor_is_410_and_says_to_resync_from_the_head(
    client_no_lifespan: TestClient, db: FakeFeedDB, pool: FakePool,
) -> None:
    _sample(db)
    db.prune_through(T, 11, 0)
    resp = client_no_lifespan.get(BASE, params={"after": "10.1"})
    assert resp.status_code == HTTPStatus.GONE
    assert "11.0" in resp.json()["detail"] and "changes/head" in resp.json()["detail"]
    assert pool.acquired == pool.released


def test_an_unknown_tenant_is_404_and_someone_elses_is_403(
    client_no_lifespan: TestClient, pool: FakePool,
) -> None:
    assert client_no_lifespan.get("/v1/zeta/changes").status_code == HTTPStatus.NOT_FOUND
    app = client_no_lifespan.app
    app.state.authenticator = StaticAuthenticator({  # type: ignore[attr-defined]
        "tok": Principal(consumer="bob", subject="user:bob", levels=(("beta", Level.READ),)),
    })
    auth = {"Authorization": "Bearer tok"}
    for path in (BASE, f"{BASE}/head", f"{BASE}/history", f"{BASE}/stream"):
        assert client_no_lifespan.get(path, headers=auth).status_code == HTTPStatus.FORBIDDEN
    assert pool.acquired == 0


def test_a_cursor_out_of_postgres_range_is_400_not_a_database_error(
    client_no_lifespan: TestClient, pool: FakePool,
) -> None:
    resp = client_no_lifespan.get(BASE, params={"after": "1.99999999999999999999"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert pool.acquired == 0


# ── GET …/changes/head and …/history ───────────────────────────────────────


def test_the_head_is_the_oldest_running_transaction(
    client_no_lifespan: TestClient, db: FakeFeedDB,
) -> None:
    db.xmin = 987654321
    body = client_no_lifespan.get(f"{BASE}/head").json()
    assert body == {"tenant": T, "cursor": "987654321.0"}


def test_history_pages_back_with_older_until_there_is_none(
    client_no_lifespan: TestClient, db: FakeFeedDB,
) -> None:
    _sample(db)
    seen: list[str] = []
    params: dict[str, Any] = {"limit": 2}
    while True:
        body = client_no_lifespan.get(f"{BASE}/history", params=params).json()
        seen += [c["cursor"] for c in body["changes"]]
        if body["older"] is None:
            break
        params["before"] = body["older"]
    assert seen == ["13.0", "12.0", "11.0", "10.1", "10.0"]


def test_history_filters_by_every_op_given(
    client_no_lifespan: TestClient, db: FakeFeedDB,
) -> None:
    _sample(db)
    body = client_no_lifespan.get(
        f"{BASE}/history", params=[("op", "delete"), ("op", "patch")],
    ).json()
    assert [c["op"] for c in body["changes"]] == ["delete", "patch"]
    assert (body["older"], body["more"]) == (None, False)


def test_history_refuses_a_malformed_before(client_no_lifespan: TestClient) -> None:
    resp = client_no_lifespan.get(f"{BASE}/history", params={"before": "x.1"})
    assert resp.status_code == HTTPStatus.BAD_REQUEST


# ── GET …/changes/stream ───────────────────────────────────────────────────


def _parse_sse(text: str) -> list[dict[str, str]]:
    """Blocks of an SSE body, in order: fields of each, ``comment`` for ``:`` lines."""
    out = []
    for block in text.split("\n\n"):
        if not block:
            continue
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                fields["comment"] = line[1:].strip()
                continue
            name, _, value = line.partition(":")
            assert name not in fields, f"field {name!r} twice in one event: {block!r}"
            fields[name] = value[1:] if value.startswith(" ") else value
        out.append(fields)
    return out


def _prune_after_reads(db: FakeFeedDB, reads: int, through: tuple[int, int]) -> None:
    """Prune the tenant's records once ``reads`` page reads have been answered."""
    real = db.fetch

    async def fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        rows = await real(sql, *args)
        if len(db.reads) >= reads:
            db.prune_through(T, *through)
        return rows

    db.fetch = fetch  # type: ignore[method-assign]


def test_the_stream_frames_each_record_as_an_event_and_ends_with_gone(
    client_no_lifespan: TestClient, db: FakeFeedDB, pool: FakePool,
) -> None:
    _sample(db)
    _prune_after_reads(db, 1, (50, 0))     # the stream then falls behind retention
    with client_no_lifespan.stream("GET", f"{BASE}/stream") as resp:
        assert resp.status_code == HTTPStatus.OK
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"
        body = resp.read().decode()
    events = _parse_sse(body)
    assert events[0] == {"retry": "2000"}
    paged = client_no_lifespan.get(BASE)    # after the prune: nothing left
    assert paged.json()["changes"] == []
    sent = [e for e in events if e.get("event") == "change"]
    assert [e["id"] for e in sent] == ["10.0", "10.1", "11.0", "12.0", "13.0"]
    for e in sent:
        assert json.loads(e["data"])["cursor"] == e["id"]
    last = events[-1]
    assert last["event"] == "gone" and "id" not in last
    gone = json.loads(last["data"])
    assert gone["status"] == HTTPStatus.GONE and "50.0" in gone["detail"]
    assert pool.acquired == pool.released


def test_the_stream_resumes_after_last_event_id_and_it_wins_over_after(
    client_no_lifespan: TestClient, db: FakeFeedDB,
) -> None:
    _sample(db)
    _prune_after_reads(db, 1, (50, 0))
    with client_no_lifespan.stream(
        "GET", f"{BASE}/stream", params={"after": "10.0"}, headers={"Last-Event-ID": "11.0"},
    ) as resp:
        events = _parse_sse(resp.read().decode())
    assert [e["id"] for e in events if e.get("event") == "change"] == ["12.0", "13.0"]


def test_a_stream_from_a_pruned_position_is_410_before_it_starts(
    client_no_lifespan: TestClient, db: FakeFeedDB,
) -> None:
    _sample(db)
    db.prune_through(T, 12, 0)
    resp = client_no_lifespan.get(f"{BASE}/stream", headers={"Last-Event-ID": "10.1"})
    assert resp.status_code == HTTPStatus.GONE
    assert not resp.headers["content-type"].startswith("text/event-stream")


def test_a_stream_with_a_malformed_last_event_id_is_400(
    client_no_lifespan: TestClient, pool: FakePool,
) -> None:
    resp = client_no_lifespan.get(
        f"{BASE}/stream", params={"after": "1.0"}, headers={"Last-Event-ID": "nope"},
    )
    assert resp.status_code == HTTPStatus.BAD_REQUEST
    assert pool.acquired == 0


# ── The event loop of one stream ───────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _Feed:
    """Stands in for ChangeFeed: each wait advances the fake clock."""

    def __init__(self, clock: _Clock, step: float) -> None:
        self.clock = clock
        self.step = step
        self.waits: list[str] = []

    async def wait(self, tenant: str, timeout: float | None = None) -> None:  # noqa: ARG002
        self.waits.append(tenant)
        self.clock.now += self.step


class _Request:
    def __init__(self, db: FakeFeedDB, feed: object, *, heartbeat_s: float = 15.0,
                 poll_s: float = 0.001, connected_checks: int = 1000) -> None:
        settings = SimpleNamespace(changes_heartbeat_s=heartbeat_s, changes_poll_s=poll_s)
        self.app = SimpleNamespace(state=SimpleNamespace(
            settings=settings, pool=FakePool(db), change_feed=feed,
        ))
        self.connected_checks = connected_checks
        self.checks = 0

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks > self.connected_checks


async def _drain(gen: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in gen]


async def _first_page(db: FakeFeedDB) -> changes.Page:
    return await changes.read(db, T, None, changes_api._STREAM_BATCH)  # type: ignore[arg-type]  # noqa: SLF001


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(changes_api, "time", c)
    return c


async def test_an_idle_stream_sends_a_keep_alive_every_heartbeat_and_no_more(
    db: FakeFeedDB, clock: _Clock,
) -> None:
    feed = _Feed(clock, step=5.0)
    req = _Request(db, feed, heartbeat_s=15.0, connected_checks=9)   # 45 s of waiting
    out = await _drain(changes_api._events(req, T, await _first_page(db)))  # type: ignore[arg-type]  # noqa: SLF001
    assert out[0] == "retry: 2000\n\n"
    assert out[1:] == [": keep-alive\n\n"] * 3
    assert feed.waits == [T] * 10


async def test_a_busy_stream_needs_no_keep_alive(db: FakeFeedDB, clock: _Clock) -> None:
    class _Busy(_Feed):
        async def wait(self, tenant: str, timeout: float | None = None) -> None:
            await super().wait(tenant, timeout)
            db.add(T, 100 + len(self.waits), 0)          # a write lands every wait

    busy = _Busy(clock, step=5.0)
    req = _Request(db, busy, heartbeat_s=15.0, connected_checks=9)
    out = await _drain(changes_api._events(req, T, await _first_page(db)))  # type: ignore[arg-type]  # noqa: SLF001
    assert not any(chunk.startswith(":") for chunk in out)
    assert sum(chunk.startswith("id: ") for chunk in out) == 9
    assert clock.now - 1000.0 >= 45.0         # well past three heartbeats


async def test_a_backlog_is_sent_without_waiting_between_full_pages(
    db: FakeFeedDB, clock: _Clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(changes_api, "_STREAM_BATCH", 3)
    for txid in range(1, 9):
        db.add(T, txid, 0)
    feed = _Feed(clock, step=1.0)
    req = _Request(db, feed, connected_checks=2)
    first = await changes.read(db, T, None, 3)  # type: ignore[arg-type]
    out = await _drain(changes_api._events(req, T, first))  # noqa: SLF001
    ids = [chunk.split("\n", 1)[0] for chunk in out if chunk.startswith("id: ")]
    assert ids == [f"id: {t}.0" for t in range(1, 9)]
    assert feed.waits == [T]          # only once the backlog was cleared


async def test_without_a_change_feed_the_stream_polls(db: FakeFeedDB) -> None:
    req = _Request(db, None, poll_s=0.001, connected_checks=2)
    out = await _drain(changes_api._events(req, T, await _first_page(db)))  # type: ignore[arg-type]  # noqa: SLF001
    assert out == ["retry: 2000\n\n"]
    assert len(db.reads) == 1 + 2


async def test_a_disconnected_client_ends_the_stream_without_another_read(
    db: FakeFeedDB, clock: _Clock,
) -> None:
    db.add(T, 1, 0)
    feed = _Feed(clock, step=1.0)
    req = _Request(db, feed, connected_checks=0)
    first = await _first_page(db)
    reads = len(db.reads)
    out = await _drain(changes_api._events(req, T, first))  # type: ignore[arg-type]  # noqa: SLF001
    assert [c for c in out if c.startswith("id: ")] == [out[1]]
    assert len(db.reads) == reads
    assert req.app.state.pool.acquired == 0


async def test_a_record_with_newlines_in_its_values_stays_one_data_line(
    db: FakeFeedDB, clock: _Clock,
) -> None:
    db.add(T, 1, 0, ids=["line\nbreak", "cr\rlf\r\n"], detail={"note": "a\n\nb"},
           consumer="x\ny")
    feed = _Feed(clock, step=1.0)
    req = _Request(db, feed, connected_checks=0)
    out = await _drain(changes_api._events(req, T, await _first_page(db)))  # type: ignore[arg-type]  # noqa: SLF001
    event = out[1]
    assert event.endswith("\n\n") and "\r" not in event
    lines = event[:-2].split("\n")
    assert [line.split(":", 1)[0] for line in lines] == ["id", "event", "data"]
    data = json.loads(lines[2].removeprefix("data: "))
    assert data["ids"] == ["line\nbreak", "cr\rlf\r\n"]
    assert data["detail"] == {"note": "a\n\nb"}


async def test_falling_behind_mid_stream_ends_with_a_gone_event(
    db: FakeFeedDB, clock: _Clock,
) -> None:
    db.add(T, 1, 0)
    db.add(T, 2, 0)
    feed = _Feed(clock, step=1.0)
    req = _Request(db, feed)
    first = await _first_page(db)
    db.prune_through(T, 3, 0)          # past where the stream got to
    out = await _drain(changes_api._events(req, T, first))  # type: ignore[arg-type]  # noqa: SLF001
    assert out[-1].startswith("event: gone\ndata: ")
    assert json.loads(out[-1].split("data: ", 1)[1])["status"] == HTTPStatus.GONE
    assert len([c for c in out if c.startswith("id: ")]) == 2

"""The change feed (graphos.changes): what is recorded, ordering, retention, HTTP.

Most tests are DB-gated.  Each uses its own tenant name, so they do not see
each other's records.  The live stream is tested over real HTTP in
``test_changes_stream.py``, and everything under concurrency in
``test_changes_soak.py``.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from http import HTTPStatus
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from pydantic import BaseModel

from graphos import changes, jobs, lookup, writes
from graphos.app import create_app
from graphos.bootstrap import TenantSpec, register_tenant
from graphos.changes import ACTOR, Actor, Cursor, CursorGoneError
from graphos.config import Settings

from .conftest import requires_db

WRITES = Path(__file__).resolve().parents[1] / "src" / "graphos" / "writes.py"


# ── pure ───────────────────────────────────────────────────────────────────


def test_cursor_round_trip_and_order() -> None:
    c = Cursor.parse("12345.67")
    assert (c.txid, c.seq, str(c)) == (12345, 67, "12345.67")
    assert Cursor(5, 9) < Cursor(6, 1) < Cursor(6, 2)
    for bad in ("", "12", "a.b", "1.2.3", "-1.2", "1.-2", "1." + "9" * 21):
        with pytest.raises(ValueError, match="cursor"):
            Cursor.parse(bad)


def test_every_write_function_records_a_change() -> None:
    """A write that forgets to record is a silent gap in every consumer."""
    not_recorded = {
        # end_community_id is an internal column, not part of any node or edge
        # a consumer reads; set_node_communities records the recompute.
        "set_edge_end_communities",
    }
    tree = ast.parse(WRITES.read_text(encoding="utf-8"))
    recorders = {"_delete_nodes_recorded"}
    missing = []
    for fn in tree.body:
        if not isinstance(fn, ast.AsyncFunctionDef) or fn.name.startswith("_"):
            continue
        calls = {
            n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            for n in ast.walk(fn) if isinstance(n, ast.Call)
        }
        if fn.name not in not_recorded and not ({"record"} | recorders) & calls:
            missing.append(fn.name)
    assert missing == []


# ── fixtures ───────────────────────────────────────────────────────────────


def _tenant() -> str:
    return f"chg_{uuid4().hex[:10]}"


async def _connect() -> asyncpg.Connection:
    """A connection outside the test pool, for a second concurrent transaction."""
    return await asyncpg.connect(os.environ["GRAPHOS_TEST_DSN"])


async def _feed(
    pool: asyncpg.Pool, tenant: str, after: Cursor | None = None,
) -> list[changes.Change]:
    out: list[changes.Change] = []
    async with pool.acquire() as conn:
        while True:
            page = await changes.read(conn, tenant, after, 1000)
            out += page.changes
            after = page.next
            if not page.more:
                return out


@contextlib.contextmanager
def _as(consumer: str):  # noqa: ANN202
    token = ACTOR.set(Actor(consumer=consumer, trace_id="t-1"))
    try:
        yield
    finally:
        ACTOR.reset(token)


async def _graph(pool: asyncpg.Pool) -> str:
    tenant = _tenant()
    await register_tenant(pool, TenantSpec(
        name=tenant, vertex_labels=("Doc", "Tag"), edge_labels=("LINKS",),
    ))
    await writes.upsert_nodes(pool, tenant=tenant, label="Doc", rows=[
        {"id": i, "props": {"team": "red"}} for i in ("a", "b", "c", "both")
    ])
    await writes.upsert_nodes(pool, tenant=tenant, label="Tag", rows=[
        {"id": "both", "props": {"team": "red"}},
    ])
    return tenant


@pytest_asyncio.fixture
async def graph(db_pool: asyncpg.Pool) -> AsyncIterator[str]:
    tenant = await _graph(db_pool)
    yield tenant
    await db_pool.execute("DELETE FROM graphos_meta.changes WHERE tenant = $1;", tenant)


def _summary(c: changes.Change) -> tuple:
    return (c.op, c.kind, c.label, c.ids, c.rev, c.count, c.detail, c.consumer, c.trace_id)


# ── what each write records ────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_each_write_records_what_it_changed(db_pool: asyncpg.Pool, graph: str) -> None:
    t = graph
    start = (await _feed(db_pool, t))[-1].cursor          # after the fixture's upserts
    with _as("ed"):
        await writes.upsert_nodes(db_pool, tenant=t, label="Doc", rows=[
            {"id": "a", "props": {"n": 1}}, {"id": "d", "props": {}},
            {"id": "a", "props": {"n": 2}},
        ])
        written, skipped = await writes.upsert_edges(
            db_pool, tenant=t, src_label="Doc", dst_label="Doc", edge_type="LINKS", rows=[
                {"src_id": "a", "dst_id": "b"}, {"src_id": "a", "dst_id": "zz"},
                {"src_id": "b", "dst_id": "c"}, {"src_id": "a", "dst_id": "b"},
            ],
        )
        assert (written, skipped) == (2, 1)
        merged = await writes.merge_node_properties(
            db_pool, tenant=t, external_id="b", properties={"x": 1},
        )
        patched = await writes.patch_node(db_pool, tenant=t, external_id="c", patch={"y": 1})
        edge = await writes.patch_edge(
            db_pool, tenant=t, edge_type="LINKS", src_id="a", dst_id="b", patch={"w": 1},
        )
        await writes.delete_edge(db_pool, tenant=t, edge_type="LINKS", src_id="a", dst_id="b")
        await writes.delete_node(db_pool, tenant=t, external_id="d")
        await writes.delete_nodes(db_pool, tenant=t, property_key="team", property_value="red")
        await writes.set_node_communities(db_pool, tenant=t, assignments=[(1, 7), (2, 7)])

    assert merged and patched and edge
    got = [_summary(c) for c in await _feed(db_pool, t, start)]
    who = ("ed", "t-1")
    assert got == [
        ("upsert", "node", "Doc", ["a", "d"], None, 2, None, *who),
        ("upsert", "edge", "LINKS", [["a", "b"], ["b", "c"]], None, 2, None, *who),
        ("merge", "node", "Doc", ["b"], merged.rev, 1, None, *who),
        ("patch", "node", "Doc", ["c"], patched.rev, 1, None, *who),
        ("patch", "edge", "LINKS", [["a", "b"]], edge.rev, 1, None, *who),
        ("delete", "edge", "LINKS", [["a", "b"]], None, 1, None, *who),
        ("delete", "node", "Doc", ["d"], None, 1, {"edges_removed": 0}, *who),
        # One record per label; edges_removed covers the whole statement (b->c).
        ("delete", "node", "Doc", ["a", "b", "both", "c"], None, 4,
         {"edges_removed": 1, "where": {"team": "red"}}, *who),
        ("delete", "node", "Tag", ["both"], None, 1,
         {"edges_removed": 1, "where": {"team": "red"}}, *who),
        ("communities", "node", None, None, None, 2, None, *who),
    ]
    edge_rec = (await _feed(db_pool, t, start))[1]
    assert (edge_rec.src_label, edge_rec.dst_label) == ("Doc", "Doc")


@requires_db
@pytest.mark.asyncio
async def test_writes_that_change_nothing_record_nothing(db_pool: asyncpg.Pool, graph: str) -> None:
    t = graph
    before = await _feed(db_pool, t)
    assert await writes.merge_node_properties(
        db_pool, tenant=t, external_id="nope", properties={"x": 1},
    ) is None
    assert await writes.patch_node(db_pool, tenant=t, external_id="nope", patch={"x": 1}) is None
    assert await writes.delete_edge(
        db_pool, tenant=t, edge_type="LINKS", src_id="a", dst_id="c",
    ) == 0
    assert await writes.delete_nodes(
        db_pool, tenant=t, property_key="team", property_value="none",
    ) == (0, 0)
    assert await writes.upsert_edges(
        db_pool, tenant=t, src_label="Doc", dst_label="Doc", edge_type="LINKS",
        rows=[{"src_id": "a", "dst_id": "zz"}],
    ) == (0, 1)
    await writes.set_node_communities(db_pool, tenant=t, assignments=[])
    assert await _feed(db_pool, t) == before


@requires_db
@pytest.mark.asyncio
async def test_a_failed_or_rolled_back_write_records_nothing(
    db_pool: asyncpg.Pool, graph: str,
) -> None:
    t = graph
    before = await _feed(db_pool, t)
    etag = None
    async with db_pool.acquire() as conn:
        node = await lookup.find_node(conn, tenant=t, external_id="a")
        assert node is not None
        etag = node.etag
    await writes.patch_node(db_pool, tenant=t, external_id="a", patch={"v": 1})
    with pytest.raises(lookup.PreconditionFailedError):
        await writes.patch_node(
            db_pool, tenant=t, external_id="a", patch={"v": 2},
            if_match=lookup.IfMatch(frozenset({etag})),
        )
    with pytest.raises(lookup.AmbiguousMatchError):
        await writes.patch_node(db_pool, tenant=t, external_id="both", patch={"v": 2})
    async with db_pool.acquire() as conn:
        with contextlib.suppress(RuntimeError):
            async with conn.transaction():
                await changes.record(conn, tenant=t, op="patch", kind="node", count=1)
                raise RuntimeError("roll back")
    after = await _feed(db_pool, t)
    assert [c.op for c in after[len(before):]] == ["patch"]     # only the successful patch


# ── who made the change ────────────────────────────────────────────────────


def _client(
    pool: asyncpg.Pool, tokens: Path | None = None, **settings: object,
) -> httpx.AsyncClient:
    if tokens is None:
        app = create_app(Settings(db_password="placeholder", auth_disabled=True, **settings))
    else:
        from graphos.auth import TokenStore
        app = create_app(Settings(db_password="placeholder", auth_tokens_file=tokens, **settings))
        app.state.token_store = TokenStore(tokens)
    app.state.pool = pool
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@requires_db
@pytest.mark.asyncio
async def test_requests_record_their_consumer_and_trace(
    db_pool: asyncpg.Pool, graph: str, tmp_path: Path,
) -> None:
    t = graph
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps(
        {"tok_app": {"consumer": "app", "tenants": [t], "access": "write"}},
    ))
    trace = "0af7651916cd43dd8448eb211c80319c"
    async with _client(db_pool, tokens) as api:
        resp = await api.patch(
            f"/v1/{t}/graph/nodes/a", json={"x": 1},
            headers={"Authorization": "Bearer tok_app",
                     "traceparent": f"00-{trace}-b7ad6b7169203331-01"},
        )
        assert resp.status_code == HTTPStatus.OK, resp.text
    last = (await _feed(db_pool, t))[-1]
    assert (last.op, last.consumer, last.trace_id) == ("patch", "app", trace)


@requires_db
@pytest.mark.asyncio
async def test_jobs_record_who_submitted_them(db_pool: asyncpg.Pool, graph: str) -> None:
    t = graph

    class Done(BaseModel):
        ok: bool = True

    async def run(pool: asyncpg.Pool, tenant: str | None, _params: dict) -> Done:
        await writes.merge_node_properties(
            pool, tenant=tenant, external_id="a", properties={"j": 1},
        )
        return Done()

    runner = jobs.JobRunner(
        db_pool, {"chg_write": jobs.JobKind(run=run)},
        concurrency=1, poll_s=0.05, heartbeat_s=0.5, retention_h=1.0,
    )
    job = await jobs.submit(db_pool, kind="chg_write", tenant=t, params={}, consumer="batch-bot")
    runner.start()
    try:
        for _ in range(200):
            done = await jobs.get(db_pool, job.id)
            if done is not None and done.status not in ('queued', 'running'):
                break
            await asyncio.sleep(0.05)
    finally:
        await runner.stop()
        await db_pool.execute("DELETE FROM graphos_meta.jobs WHERE id = $1;", job.id)
    assert done is not None and done.status == 'succeeded', done
    last = (await _feed(db_pool, t))[-1]
    assert (last.op, last.consumer, last.trace_id) == ("merge", "batch-bot", f"job:{job.id}")


@requires_db
@pytest.mark.asyncio
async def test_rule_resolution_records_one_summary(db_pool: asyncpg.Pool) -> None:
    t = _tenant()
    async with _client(db_pool) as api:
        resp = await api.post("/v1/tenants", json={
            "name": t, "vertex_labels": ["Doc"],
            "edge_labels": [{"name": "TAGGED", "resolve_rule": {
                "src_has_array": "tags", "dst_value_from": "name",
            }}],
        })
        assert resp.status_code == HTTPStatus.OK, resp.text
        await api.post(f"/v1/{t}/graph/nodes", json={"nodes": [
            {"id": "d1", "label": "Doc", "properties": {"tags": ["x", "y"]}},
            {"id": "x", "label": "Doc", "properties": {"name": "x"}},
            {"id": "y", "label": "Doc", "properties": {"name": "y"}},
        ]})
        resp = await api.post(f"/v1/{t}/graph/edges:resolve", json={})
        assert resp.json()["total_edges_written"] == 2, resp.text
    last = (await _feed(db_pool, t))[-1]
    assert (last.op, last.kind, last.label, last.ids, last.count) == (
        "resolve", "edge", "TAGGED", None, 2,
    )


# ── ordering and visibility ────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_a_late_commit_is_never_skipped(db_pool: asyncpg.Pool) -> None:
    """A starts first, B commits first: a reader polling in between must
    still see A, then B, and nothing twice."""
    t = _tenant()
    a, b = await _connect(), await _connect()
    try:
        tx_a, tx_b = a.transaction(), b.transaction()
        await tx_a.start()
        await changes.record(a, tenant=t, op="upsert", kind="node", ids=["A"], count=1)
        await tx_b.start()
        await changes.record(b, tenant=t, op="upsert", kind="node", ids=["B"], count=1)
        await tx_b.commit()

        assert await _feed(db_pool, t) == []   # B is committed but must wait for A
        async with db_pool.acquire() as conn:
            cursor = (await changes.read(conn, t, None, 10)).next

        await tx_a.commit()
        seen = await _feed(db_pool, t, cursor)
        assert [c.ids for c in seen] == [["A"], ["B"]]
        assert seen[0].cursor < seen[1].cursor
    finally:
        await a.close()
        await b.close()


@requires_db
@pytest.mark.asyncio
async def test_order_is_numeric_across_powers_of_ten(db_pool: asyncpg.Pool) -> None:
    """Transaction ids cross 9 -> 10, 99 -> 100, ...: a text sort anywhere
    would put "10000" before "9999" and the pages would skip records."""
    t = _tenant()
    txids = [9, 10, 99, 100, 999, 1000, 9999, 10000, 99999, 100000]
    for x in reversed(txids):            # inserted out of order on purpose
        await db_pool.execute(
            "INSERT INTO graphos_meta.changes (tenant, txid, op, kind, ids, count) "
            "VALUES ($1, $2::text::xid8, 'upsert', 'node', $3::jsonb, 1);",
            t, str(x), json.dumps([str(x)]),
        )
    for limit in (1, 2, 3, 1000):
        seen, after = [], None
        async with db_pool.acquire() as conn:
            while True:
                page = await changes.read(conn, t, after, limit)
                seen += [c.cursor.txid for c in page.changes]
                after = page.next
                if not page.more:
                    break
        assert seen == txids, limit


@requires_db
@pytest.mark.asyncio
async def test_head_starts_a_sync_without_losing_running_writes(db_pool: asyncpg.Pool) -> None:
    t = _tenant()
    async with db_pool.acquire() as conn:
        await changes.record(conn, tenant=t, op="upsert", kind="node", ids=["old"], count=1)
    other = await _connect()
    try:
        tx = other.transaction()
        await tx.start()
        await changes.record(other, tenant=t, op="upsert", kind="node", ids=["running"], count=1)
        async with db_pool.acquire() as conn:
            # Committed after "running" started: the newest finished
            # transaction is now younger than the running one, so a head
            # taken from anything but the oldest running transaction would
            # land after "running" and lose it.
            await changes.record(conn, tenant=t, op="upsert", kind="node", ids=["between"], count=1)
            head = await changes.head(conn)
            await changes.record(conn, tenant=t, op="upsert", kind="node", ids=["new"], count=1)
        await tx.commit()
    finally:
        await other.close()
    after_head = [c.ids[0] for c in await _feed(db_pool, t, head)]
    assert after_head == ["running", "between", "new"]   # "old" was before the head


# ── retention ──────────────────────────────────────────────────────────────


async def _age(pool: asyncpg.Pool, tenant: str, hours: float) -> None:
    await pool.execute(
        "UPDATE graphos_meta.changes SET at = at - make_interval(secs => $2) WHERE tenant = $1;",
        tenant, hours * 3600,
    )


@requires_db
@pytest.mark.asyncio
async def test_pruned_cursors_are_gone_and_newer_ones_are_not(db_pool: asyncpg.Pool) -> None:
    t = _tenant()
    async with db_pool.acquire() as conn:
        for i in range(5):
            await changes.record(conn, tenant=t, op="upsert", kind="node", ids=[f"n{i}"], count=1)
    first = await _feed(db_pool, t)
    await _age(db_pool, t, 2)
    async with db_pool.acquire() as conn:
        await changes.record(conn, tenant=t, op="upsert", kind="node", ids=["kept"], count=1)

    pruned = await changes.prune(db_pool, retention_h=1, batch=2)    # several batches
    assert pruned >= 5
    async with db_pool.acquire() as conn:
        assert await changes.horizon(conn, t) == first[-1].cursor
        with pytest.raises(CursorGoneError):
            await changes.read(conn, t, first[2].cursor, 10)
        assert [c.ids for c in (await changes.read(conn, t, first[-1].cursor, 10)).changes] == [
            ["kept"],
        ]
        assert [c.ids for c in (await changes.read(conn, t, None, 10)).changes] == [["kept"]]
    assert await changes.prune(db_pool, retention_h=1) == 0


@requires_db
@pytest.mark.asyncio
async def test_pruning_never_passes_a_running_transaction(db_pool: asyncpg.Pool) -> None:
    t = _tenant()
    other = await _connect()
    try:
        tx = other.transaction()
        await tx.start()
        await changes.record(other, tenant=t, op="upsert", kind="node", ids=["slow"], count=1)
        async with db_pool.acquire() as conn:
            await changes.record(conn, tenant=t, op="upsert", kind="node", ids=["fast"], count=1)
        await db_pool.execute(
            "UPDATE graphos_meta.changes SET at = at - interval '2 hours' WHERE tenant = $1;", t,
        )
        await changes.prune(db_pool, retention_h=1)
        await other.execute(
            "UPDATE graphos_meta.changes SET at = at - interval '2 hours' WHERE tenant = $1;", t,
        )
        await tx.commit()
    finally:
        await other.close()
    # "fast" committed after "slow" started, so neither was eligible yet:
    # nothing behind a running transaction is pruned.
    assert [c.ids for c in await _feed(db_pool, t)] == [["slow"], ["fast"]]


@requires_db
@pytest.mark.asyncio
async def test_concurrent_pruners_agree(db_pool: asyncpg.Pool) -> None:
    t = _tenant()
    async with db_pool.acquire() as conn:
        for i in range(40):
            await changes.record(conn, tenant=t, op="upsert", kind="node", ids=[f"n{i}"], count=1)
    last = (await _feed(db_pool, t))[-1].cursor
    await _age(db_pool, t, 2)
    counts = await asyncio.gather(*(changes.prune(db_pool, 1, batch=3) for _ in range(4)))
    assert sum(counts) >= 40
    async with db_pool.acquire() as conn:
        assert await changes.horizon(conn, t) == last
    assert await _feed(db_pool, t) == []


# ── HTTP: pages and head ───────────────────────────────────────────────────


@requires_db
@pytest.mark.asyncio
async def test_pages_over_http(db_pool: asyncpg.Pool, graph: str) -> None:
    t = graph
    async with _client(db_pool) as api:
        await api.post(f"/v1/{t}/graph/nodes/a/properties", json={"properties": {"k": 1}})
        await api.request(
            "DELETE", f"/v1/{t}/graph/edges/LINKS", params={"src": "a", "dst": "b"},
        )
        await api.patch(f"/v1/{t}/graph/nodes/b", json={"k": 2})

        seen, after = [], None
        while True:
            params = {"limit": 1} | ({"after": after} if after else {})
            body = (await api.get(f"/v1/{t}/changes", params=params)).json()
            seen += body["changes"]
            after = body["next"]
            if not body["more"]:
                break
        assert [c["op"] for c in seen] == ["upsert", "upsert", "merge", "patch"]
        assert seen[2]["ids"] == ["a"] and seen[2]["edges"] is None and seen[2]["rev"] == 2
        again = (await api.get(f"/v1/{t}/changes", params={"after": after})).json()
        assert (again["changes"], again["next"], again["more"]) == ([], after, False)

        head = (await api.get(f"/v1/{t}/changes/head")).json()["cursor"]
        await api.patch(f"/v1/{t}/graph/nodes/c", json={"k": 3})
        new = (await api.get(f"/v1/{t}/changes", params={"after": head})).json()["changes"]
        assert [(c["op"], c["ids"]) for c in new] == [("patch", ["c"])]

        bad = await api.get(f"/v1/{t}/changes", params={"after": "nope"})
        assert bad.status_code == HTTPStatus.BAD_REQUEST
        too_many = await api.get(f"/v1/{t}/changes", params={"limit": 5000})
        assert too_many.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert (await api.get("/v1/not_registered/changes")).status_code == HTTPStatus.NOT_FOUND

        await _age(db_pool, t, 400)
        await changes.prune(db_pool, retention_h=1)
        gone = await api.get(f"/v1/{t}/changes", params={"after": "1.1"})
        assert gone.status_code == HTTPStatus.GONE
        assert (await api.get(f"/v1/{t}/changes")).status_code == HTTPStatus.OK


@requires_db
@pytest.mark.asyncio
async def test_edge_records_over_http(db_pool: asyncpg.Pool, graph: str) -> None:
    t = graph
    async with _client(db_pool) as api:
        await api.post(f"/v1/{t}/graph/edges", json={"edges": [
            {"src_id": "a", "src_label": "Doc", "dst_id": "c", "dst_label": "Doc", "type": "LINKS"},
        ]})
        last = (await api.get(f"/v1/{t}/changes", params={"limit": 1000})).json()["changes"][-1]
    assert (last["kind"], last["ids"], last["edges"]) == ("edge", None, [["a", "c"]])
    assert (last["src_label"], last["dst_label"]) == ("Doc", "Doc")

"""Client-side tests against a mocked HTTP transport.

We use httpx's ``MockTransport`` to intercept requests and assert that the
client builds them correctly (URL, headers, body shape) and parses
responses into the right Pydantic types. No real graphos required.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from graphos_client import (
    BearerAuth,
    EdgeUpsert,
    GraphosClient,
    GraphosError,
    NodeUpsert,
    PasswordAuth,
    SearchHit,
)


def _mock_transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_search_request_shape() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "tenant": "demo", "query": "auth", "label": None,
                "limit": 10, "truncated": False, "elapsed_ms": 4,
                "nodes": [],
            },
        )

    cli = GraphosClient(base_url="http://x", token="tok", tenant="demo")
    cli._http = httpx.AsyncClient(
        base_url="http://x",
        transport=_mock_transport(handler),
        headers={"Authorization": "Bearer tok"},
    )

    resp = await cli.search("auth", limit=10)
    assert resp.tenant == "demo"
    assert "/v1/demo/graph/search" in captured["url"]
    assert "q=auth" in captured["url"]
    assert "limit=10" in captured["url"]
    assert captured["auth"] == "Bearer tok"

    await cli.close()


@pytest.mark.asyncio
async def test_upsert_nodes_posts_json() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["method"] = request.method
        return httpx.Response(
            200,
            json={
                "tenant": "demo", "nodes_upserted": 2,
                "by_label": {"Document": 2}, "elapsed_ms": 9,
            },
        )

    cli = GraphosClient(base_url="http://x", token="tok", tenant="demo")
    cli._http = httpx.AsyncClient(
        base_url="http://x",
        transport=_mock_transport(handler),
        headers={"Authorization": "Bearer tok"},
    )

    resp = await cli.upsert_nodes([
        NodeUpsert(id="a", label="Document"),
        NodeUpsert(id="b", label="Document", properties={"name": "beta"}),
    ])
    assert resp.nodes_upserted == 2
    assert captured["method"] == "POST"
    assert len(captured["body"]["nodes"]) == 2
    assert captured["body"]["nodes"][1]["properties"]["name"] == "beta"

    await cli.close()


@pytest.mark.asyncio
async def test_bulk_upsert_streams_ndjson() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type")
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "tenant": "demo", "rows_received": 2, "rows_invalid": 0,
                "nodes_upserted": 2, "by_label": {"Document": 2},
                "errors": [], "elapsed_ms": 12,
            },
        )

    cli = GraphosClient(base_url="http://x", token="tok", tenant="demo")
    cli._http = httpx.AsyncClient(
        base_url="http://x",
        transport=_mock_transport(handler),
        headers={"Authorization": "Bearer tok"},
    )

    resp = await cli.bulk_upsert_nodes([
        NodeUpsert(id="a", label="Document"),
        NodeUpsert(id="b", label="Document"),
    ])
    assert resp.rows_received == 2
    assert captured["content_type"] == "application/x-ndjson"
    # Each line is its own JSON object.
    lines = captured["body"].split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == "a"

    await cli.close()


@pytest.mark.asyncio
async def test_404_raises_graphos_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "tenant 'x' not registered"})

    cli = GraphosClient(base_url="http://x", token="tok", tenant="x")
    cli._http = httpx.AsyncClient(
        base_url="http://x",
        transport=_mock_transport(handler),
        headers={"Authorization": "Bearer tok"},
    )

    with pytest.raises(GraphosError) as ei:
        await cli.search("anything")
    assert ei.value.status_code == 404
    assert "not registered" in ei.value.detail

    await cli.close()


@pytest.mark.asyncio
async def test_retry_on_503() -> None:
    """503 should be retried once, then succeed."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(503, json={"detail": "not ready"})
        return httpx.Response(
            200,
            json={"tenant": "t", "query": "x", "label": None, "limit": 10,
                  "truncated": False, "elapsed_ms": 1, "nodes": []},
        )

    cli = GraphosClient(
        base_url="http://x", token="tok", tenant="t",
        retries=3, retry_backoff_s=0.001,
    )
    cli._http = httpx.AsyncClient(
        base_url="http://x",
        transport=_mock_transport(handler),
        headers={"Authorization": "Bearer tok"},
    )
    resp = await cli.search("x")
    assert resp.tenant == "t"
    assert state["calls"] == 2

    await cli.close()


@pytest.mark.asyncio
async def test_expand_passes_rel_types_as_repeated_query() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "tenant": "t", "node_id": "a", "max_hops": 3,
                "direction": "out", "rel_types": ["CITES"], "limit": 100,
                "truncated": False, "elapsed_ms": 7, "paths": [],
            },
        )

    cli = GraphosClient(base_url="http://x", token="tok", tenant="t")
    cli._http = httpx.AsyncClient(
        base_url="http://x",
        transport=_mock_transport(handler),
        headers={"Authorization": "Bearer tok"},
    )
    await cli.expand("a", max_hops=3, direction="out", rel_types=["CITES"])
    # httpx encodes list params as repeated keys: ?rel_types=CITES
    assert "rel_types=CITES" in captured["url"]

    await cli.close()


@pytest.mark.asyncio
async def test_aggregate_edges_uses_cross_team_shape() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "tenant": "t", "group_by_src": "team",
                "group_by_dst": "team", "rel_types": ["CITES"],
                "truncated": False, "elapsed_ms": 5, "groups": [],
            },
        )

    cli = GraphosClient(base_url="http://x", token="tok", tenant="t")
    cli._http = httpx.AsyncClient(
        base_url="http://x",
        transport=_mock_transport(handler),
        headers={"Authorization": "Bearer tok"},
    )
    await cli.aggregate_edges(
        group_by_src="team",
        group_by_dst="team",
        rel_types=["CITES"],
        require_different=True,
    )
    assert captured["body"]["require_different"] is True
    assert captured["body"]["group_by_src"] == "team"
    assert captured["body"]["rel_types"] == ["CITES"]

    await cli.close()


@pytest.mark.asyncio
async def test_recompute_communities_with_node_filter() -> None:
    """node_filter scopes Louvain to a property-defined subgraph."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "tenant": "t", "n_nodes": 3, "n_edges_scanned": 3,
                "n_communities": 1, "n_assigned": 3, "n_singletons": 0,
                "largest_community_size": 3, "modularity": 0.0,
                "edge_stream_ms": 1, "igraph_ms": 0, "writeback_ms": 1,
                "elapsed_ms": 3, "dry_run": False,
            },
        )

    cli = GraphosClient(base_url="http://x", token="tok", tenant="t")
    cli._http = httpx.AsyncClient(
        base_url="http://x",
        transport=_mock_transport(handler),
        headers={"Authorization": "Bearer tok"},
    )

    # Scalar filter (per-project recompute).
    await cli.recompute_communities(node_filter={"project_id": "demo"})
    assert captured["body"]["node_filter"] == {"project_id": "demo"}

    # List filter (multi-project).
    await cli.recompute_communities(node_filter={"project_id": ["a", "b"]})
    assert captured["body"]["node_filter"] == {"project_id": ["a", "b"]}

    # No filter → field absent (so it doesn't override server defaults).
    await cli.recompute_communities()
    assert "node_filter" not in captured["body"]

    await cli.close()


@pytest.mark.asyncio
async def test_upsert_edges_round_trip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tenant": "t", "edges_upserted": 1, "edges_skipped": 0,
                "by_type": {"CITES": 1}, "elapsed_ms": 4,
            },
        )

    cli = GraphosClient(base_url="http://x", token="tok", tenant="t")
    cli._http = httpx.AsyncClient(
        base_url="http://x",
        transport=_mock_transport(handler),
        headers={"Authorization": "Bearer tok"},
    )
    resp = await cli.upsert_edges([
        EdgeUpsert(
            src_id="a", src_label="Document",
            dst_id="b", dst_label="Document", type="CITES",
        ),
    ])
    assert resp.edges_upserted == 1
    assert resp.by_type == {"CITES": 1}

    await cli.close()


def _counting_client(status: int) -> tuple[GraphosClient, list[str]]:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(status, json={"detail": "boom"})

    cli = GraphosClient(
        base_url="http://x", token="tok", tenant="demo", retries=2, retry_backoff_s=0.0,
    )
    cli._http = httpx.AsyncClient(base_url="http://x", transport=_mock_transport(handler))
    return cli, calls


@pytest.mark.asyncio
async def test_retries_idempotent_reads_on_5xx() -> None:
    cli, calls = _counting_client(502)
    with pytest.raises(GraphosError):
        await cli.search("x")
    assert len(calls) == 3          # first try + 2 retries
    await cli.close()


@pytest.mark.asyncio
async def test_never_retries_long_operations_or_timeouts() -> None:
    cli, calls = _counting_client(500)
    with pytest.raises(GraphosError):
        await cli.recompute_communities()
    with pytest.raises(GraphosError):
        await cli.resolve_edges()
    assert len(calls) == 2          # one call each, no retries
    await cli.close()

    cli, calls = _counting_client(504)
    with pytest.raises(GraphosError):
        await cli.search("x")
    assert len(calls) == 1          # a statement timeout is not retried
    await cli.close()


def _job(status: str, **extra) -> dict:
    return {"id": "8c1f3f6e-2f0a-4c55-9a64-7c1f0e2b9a10", "kind": "recompute_communities",
            "tenant": "demo", "status": status, "params": {}, "created_at": "2026-10-05T10:00:00Z",
            **extra}


@pytest.mark.asyncio
async def test_submit_sends_prefer_and_is_never_retried() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(503, json={"detail": "down"})

    cli = GraphosClient(base_url="http://x", tenant="demo", transport=_mock_transport(handler),
                        retries=3, retry_backoff_s=0)
    with pytest.raises(GraphosError):
        await cli.submit_recompute_communities(rel_types=["CITES"])
    assert len(seen) == 1                               # a retry could queue it twice
    assert seen[0].headers["prefer"] == "respond-async"
    assert seen[0].url.path == "/v1/demo/recompute-communities"
    assert json.loads(seen[0].content)["rel_types"] == ["CITES"]
    await cli.close()


@pytest.mark.asyncio
async def test_wait_for_job_polls_until_finished() -> None:
    states = iter(["queued", "running", "succeeded"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json=_job("queued"))
        status = next(states)
        result = {"n_communities": 3} if status == "succeeded" else None
        return httpx.Response(200, json=_job(status, result=result))

    cli = GraphosClient(base_url="http://x", tenant="demo", transport=_mock_transport(handler))
    job = await cli.submit_resolve_edges()
    assert job.status == "queued" and not job.done
    done = await cli.wait_for_job(job.id, poll_s=0)
    assert done.status == "succeeded" and done.result == {"n_communities": 3}
    await cli.close()


@pytest.mark.asyncio
async def test_list_and_cancel_jobs() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/jobs":
            return httpx.Response(200, json={"jobs": [_job("running")]})
        return httpx.Response(200, json=_job("running", cancel_requested=True))

    cli = GraphosClient(base_url="http://x", tenant="demo", transport=_mock_transport(handler))
    jobs = await cli.list_jobs(tenant="demo", status="running", limit=5)
    assert [j.status for j in jobs] == ["running"]
    assert dict(seen[0].url.params) == {"tenant": "demo", "status": "running", "limit": "5"}
    cancelled = await cli.cancel_job(jobs[0].id)
    assert cancelled.cancel_requested
    assert seen[1].method == "POST" and seen[1].url.path.endswith(":cancel")
    await cli.close()


_EDGE = {
    "tenant": "demo", "type": "CITES", "src_id": "a", "src_label": "Doc",
    "dst_id": "b/1", "dst_label": "Doc", "properties": {"w": 1}, "elapsed_ms": 1,
}


@pytest.mark.asyncio
async def test_patch_node_sends_a_merge_patch() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "tenant": "demo", "external_id": "a/1", "label": "Doc",
            "properties": {"id": "a/1", "x": 2}, "elapsed_ms": 1,
        })

    cli = GraphosClient(base_url="http://x", tenant="demo", transport=_mock_transport(handler))
    node = await cli.patch_node("a/1", {"x": 2, "old": None}, label="Doc")
    assert node.properties == {"id": "a/1", "x": 2}
    (req,) = seen
    assert req.method == "PATCH"
    assert req.url.raw_path == b"/v1/demo/graph/nodes/a%2F1?label=Doc"
    assert req.headers["content-type"] == "application/merge-patch+json"
    assert json.loads(req.content) == {"x": 2, "old": None}
    await cli.close()


@pytest.mark.asyncio
async def test_edge_get_patch_delete() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json={
                "tenant": "demo", "type": "CITES", "src_id": "a", "dst_id": "b/1",
                "edges_deleted": 1, "elapsed_ms": 1,
            })
        if request.url.params.get("dst") == "missing":
            return httpx.Response(404, json={"detail": "no CITES edge"})
        return httpx.Response(200, json=_EDGE)

    cli = GraphosClient(base_url="http://x", tenant="demo", transport=_mock_transport(handler))
    edge = await cli.get_edge("CITES", "a", "b/1", dst_label="Doc")
    assert edge is not None and edge.properties == {"w": 1}
    assert seen[0].url.path == "/v1/demo/graph/edges/CITES"
    assert dict(seen[0].url.params) == {"src": "a", "dst": "b/1", "dst_label": "Doc"}
    assert await cli.get_edge("CITES", "a", "missing") is None

    await cli.patch_edge("CITES", "a", "b/1", {"w": None})
    assert seen[2].method == "PATCH"
    assert seen[2].headers["content-type"] == "application/merge-patch+json"
    assert json.loads(seen[2].content) == {"w": None}

    deleted = await cli.delete_edge("CITES", "a", "b/1", src_label="Doc")
    assert deleted.edges_deleted == 1
    assert dict(seen[3].url.params) == {"src": "a", "dst": "b/1", "src_label": "Doc"}
    await cli.close()


@pytest.mark.asyncio
async def test_if_match_is_sent_and_etag_read() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("if-match") == '"1.1"':
            return httpx.Response(412, json={"detail": "If-Match failed"})
        return httpx.Response(200, json={
            "tenant": "demo", "external_id": "a", "label": "Doc",
            "properties": {"id": "a"}, "rev": 3, "etag": '"1.3"', "elapsed_ms": 1,
        })

    cli = GraphosClient(base_url="http://x", tenant="demo", transport=_mock_transport(handler))
    node = await cli.patch_node("a", {"x": 1}, if_match='"1.2"')
    assert (node.rev, node.etag) == (3, '"1.3"')
    assert seen[0].headers["if-match"] == '"1.2"'
    with pytest.raises(GraphosError) as exc:
        await cli.patch_node("a", {"x": 1}, if_match='"1.1"')
    assert exc.value.status_code == 412
    await cli.patch_node("a", {"x": 1})
    assert "if-match" not in seen[2].headers
    await cli.close()


@pytest.mark.asyncio
async def test_find_paths() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "tenant": "demo", "src": "a", "dst": "c", "direction": "out", "hops": 1,
            "paths": [{"hops": 1,
                       "nodes": [{"id": "a", "label": "P"}, {"id": "c", "label": "P"}],
                       "edges": [{"type": "KNOWS", "src_id": "a", "dst_id": "c"}]}],
            "truncated": False, "visited": 2, "elapsed_ms": 1,
        })

    cli = GraphosClient(base_url="http://x", tenant="demo", transport=_mock_transport(handler))
    found = await cli.find_paths("a", "c", direction="out", rel_types=["KNOWS"], limit=3)
    assert found.hops == 1 and found.paths[0].edges[0].type == "KNOWS"
    assert seen[0].url.path == "/v1/demo/graph/paths"
    assert json.loads(seen[0].content) == {
        "src": "a", "dst": "c", "direction": "out", "max_hops": 4, "limit": 3,
        "include_properties": False, "rel_types": ["KNOWS"],
    }
    await cli.close()


def _change(n: int, op: str = "upsert") -> dict:
    return {"cursor": f"100.{n}", "at": "2026-10-06T08:00:00Z", "op": op, "kind": "node",
            "label": "Doc", "ids": [f"n{n}"], "count": 1}


@pytest.mark.asyncio
async def test_changes_pages_until_caught_up() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        after = request.url.params.get("after")
        if after is None:
            return httpx.Response(200, json={"tenant": "demo", "changes": [_change(1), _change(2)],
                                             "next": "100.2", "more": True})
        return httpx.Response(200, json={"tenant": "demo", "changes": [_change(3)],
                                         "next": "100.3", "more": False})

    cli = GraphosClient(base_url="http://x", tenant="demo", transport=_mock_transport(handler))
    got = [c.cursor async for c in cli.iter_changes(page_size=2)]
    assert got == ["100.1", "100.2", "100.3"]
    assert dict(seen[1].url.params) == {"limit": "2", "after": "100.2"}
    assert seen[0].url.path == "/v1/demo/changes"
    await cli.close()


async def test_changes_history_reads_backwards() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "tenant": "demo", "changes": [_change(3), _change(2)], "older": "100.2", "more": True,
        })

    cli = GraphosClient(base_url="http://x", tenant="demo", transport=_mock_transport(handler))
    page = await cli.changes_history("100.4", ops=["merge", "schema"], limit=2)
    assert [c.cursor for c in page.changes] == ["100.3", "100.2"]
    assert page.older == "100.2" and page.more
    assert seen[0].url.path == "/v1/demo/changes/history"
    assert seen[0].url.params.get_list("op") == ["merge", "schema"]
    assert seen[0].url.params["before"] == "100.4"
    await cli.close()


async def test_list_jobs_takes_several_states() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"jobs": [_job("running", sync=True)]})

    cli = GraphosClient(base_url="http://x", tenant="demo", transport=_mock_transport(handler))
    (job,) = await cli.list_jobs(tenant="demo", status=["queued", "running"])
    assert job.sync
    assert seen[0].url.params.get_list("status") == ["queued", "running"]
    await cli.close()


def _sse(*chunks: str):  # noqa: ANN202
    async def body():  # noqa: ANN202
        for chunk in chunks:
            yield chunk.encode()
    return body()


@pytest.mark.asyncio
async def test_stream_parses_resumes_and_stops_on_gone() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        n = len(calls)
        if n == 1:   # two events split at odd places, then the connection ends
            e1, e2 = json.dumps(_change(1)), json.dumps(_change(2))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(
                "retry: 2000\n\n: keep-alive\n\nid: 100.1\nevent: change\nda",
                f"ta: {e1}\n\nid: 100.2\nevent: change\ndata: {e2[:10]}",
                f"{e2[10:]}\n\n",
            ))
        if n == 2:
            raise httpx.ConnectError("refused")
        if n == 3:
            return httpx.Response(503, json={"detail": "not ready"})
        if n == 4:
            e3 = json.dumps(_change(3))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(
                f"id: 100.3\nevent: change\ndata: {e3}\n\n",
                'event: gone\ndata: {"status": 410, "detail": "pruned"}\n\n',
            ))
        raise AssertionError("no further request expected")

    cli = GraphosClient(base_url="http://x", tenant="demo", retry_backoff_s=0.001,
                        transport=_mock_transport(handler))
    got = []
    with pytest.raises(GraphosError) as exc:
        async for change in cli.stream_changes("100.0"):
            got.append(change.cursor)
    assert exc.value.status_code == 410 and "pruned" in str(exc.value)
    assert got == ["100.1", "100.2", "100.3"]
    assert [r.headers.get("last-event-id") for r in calls] == ["100.0", "100.2", "100.2", "100.2"]
    assert calls[0].url.path == "/v1/demo/changes/stream"
    await cli.close()


@pytest.mark.asyncio
async def test_stream_does_not_retry_a_client_error() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(403, json={"detail": "token has no access to tenant 'demo'"})

    cli = GraphosClient(base_url="http://x", tenant="demo", retry_backoff_s=0.001,
                        transport=_mock_transport(handler))
    with pytest.raises(GraphosError) as exc:
        async for _ in cli.stream_changes():
            pass
    assert exc.value.status_code == 403 and len(calls) == 1
    assert "last-event-id" not in calls[0].headers
    await cli.close()


@pytest.mark.asyncio
async def test_ranked_search_and_search_index_registration() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"name": "demo"})
        return httpx.Response(200, json={
            "tenant": "demo", "query": "user svc", "label": None, "index": "names",
            "limit": 5, "truncated": False, "elapsed_ms": 3,
            "nodes": [{"id": 7, "label": "Class", "properties": {"name": "UserService"},
                       "score": 0.97, "matched": "name"}],
        })

    cli = GraphosClient(base_url="http://x", token="tok", tenant="demo")
    cli._http = httpx.AsyncClient(base_url="http://x", transport=_mock_transport(handler))
    await cli.register_tenant(vertex_labels=["Class"], search_indexes={
        "names": {"properties": ["name"], "analyzer": "identifier"},
    })
    resp = await cli.search("user svc", index="names", limit=5, fuzzy=True, min_score=0.5)
    assert json.loads(captured[0].content)["search_indexes"] == {
        "names": {"properties": ["name"], "analyzer": "identifier"},
    }
    params = dict(captured[1].url.params)
    assert params == {"q": "user svc", "limit": "5", "index": "names", "fuzzy": "true",
                      "min_score": "0.5"}
    assert resp.index == "names"
    hit = resp.nodes[0]
    assert isinstance(hit, SearchHit) and (hit.score, hit.matched) == (0.97, "name")

    # Plain search sends none of the ranked-search parameters.
    await cli.search("auth")
    assert set(captured[2].url.params) == {"q", "limit"}
    await cli.register_tenant(vertex_labels=["Class"])
    assert "search_indexes" not in json.loads(captured[3].content)
    await cli.close()


@pytest.mark.asyncio
async def test_me_and_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/me":
            return httpx.Response(200, json={"consumer": "ada", "admin": False,
                                             "tenants": ["demo"], "levels": {"demo": "read"}})
        assert request.url.path == "/v1/demo/schema"
        return httpx.Response(200, json={"name": "demo", "vertex_labels": ["Person"],
                                         "search_indexes": {"names": {"analyzer": "identifier"}}})

    cli = GraphosClient(base_url="http://x", token="tok", tenant="demo")
    cli._http = httpx.AsyncClient(base_url="http://x", transport=_mock_transport(handler))
    me = await cli.me()
    assert (me.consumer, me.levels, me.tenants) == ("ada", {"demo": "read"}, ["demo"])
    schema = await cli.schema()
    assert schema.vertex_labels == ["Person"] and "names" in schema.search_indexes
    # A server that predates declared endpoints sends neither field.
    assert schema.edge_endpoints == {} and schema.strict_endpoints is False
    await cli.close()


@pytest.mark.asyncio
async def test_declared_endpoints_are_sent_served_and_removed() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/v1/demo/schema":
            return httpx.Response(200, json={
                "name": "demo", "strict_endpoints": True,
                "edge_endpoints": {"CALLS": [{"src": "Function", "dst": "Function"}]},
            })
        return httpx.Response(200, json={"tenant": "demo"})

    cli = GraphosClient(base_url="http://x", token="tok", tenant="demo")
    cli._http = httpx.AsyncClient(base_url="http://x", transport=_mock_transport(handler))
    await cli.register_tenant(
        edge_endpoints={"CALLS": [("Function", "Function")],
                        "DEFINES": [{"src": "File", "dst": "Symbol"}]},
        strict_endpoints=True,
    )
    body = json.loads(captured[0].content)
    assert body["edge_endpoints"] == {"CALLS": [["Function", "Function"]],
                                      "DEFINES": [{"src": "File", "dst": "Symbol"}]}
    assert body["strict_endpoints"] is True
    # Without them, neither field is sent: the stored ones stay as they are.
    await cli.register_tenant(vertex_labels=["Function"])
    assert {"edge_endpoints", "strict_endpoints"}.isdisjoint(json.loads(captured[1].content))

    schema = await cli.schema()
    assert schema.strict_endpoints is True
    assert [(p.src, p.dst) for p in schema.edge_endpoints["CALLS"]] == [("Function", "Function")]

    await cli.remove_edge_endpoint("CALLS", "Function", "Function")
    removed = captured[-1]
    assert (removed.method, removed.url.path) == ("DELETE", "/v1/tenants/demo/edge_endpoints")
    assert dict(removed.url.params) == {"type": "CALLS", "src": "Function", "dst": "Function"}
    await cli.close()


@pytest.mark.asyncio
async def test_graph_map_hubs_community_map_and_property_search() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        path = request.url.path
        if path == "/v1/demo/graph/map":
            return httpx.Response(200, json={
                "tenant": "demo", "estimated": True, "elapsed_ms": 9,
                "labels": [{"label": "Function", "count": 10}],
                "links": [{"src_label": "Function", "type": "CALLS", "dst_label": "Function",
                           "count": 40, "estimated": True}],
            })
        if path == "/v1/demo/graph/map/hubs":
            return httpx.Response(200, json={
                "tenant": "demo", "label": "Function", "elapsed_ms": 3,
                "nodes": [{"id": "f0", "label": "Function", "properties": {"name": "f0"},
                           "degree": 5}],
            })
        if path == "/v1/demo/graph/communities/map":
            return httpx.Response(200, json={
                "tenant": "demo", "available": True, "computed_at": "2026-10-07T10:00:00Z",
                "modularity": 0.5, "n_communities": 1,
                "communities": [{"community_id": 4, "size": 6, "labels": {"Function": 6},
                                 "hub": {"id": "f0", "label": "Function", "properties": {},
                                         "degree": 5},
                                 "internal_edges": 15}],
                "links": [],
            })
        assert path == "/v1/demo/graph/search"
        return httpx.Response(200, json={"tenant": "demo", "query": "src", "limit": 20,
                                         "truncated": False, "elapsed_ms": 1, "nodes": []})

    cli = GraphosClient(base_url="http://x", token="tok", tenant="demo")
    cli._http = httpx.AsyncClient(base_url="http://x", transport=_mock_transport(handler))
    graph_map = await cli.graph_map(exact=True)
    assert graph_map.links[0].count == 40 and "exact=true" in seen[-1]
    hubs = await cli.map_hubs("Function", limit=5)
    assert hubs.nodes[0].degree == 5 and "label=Function" in seen[-1] and "limit=5" in seen[-1]
    groups = await cli.community_map(limit=10)
    assert groups.available and groups.communities[0].hub is not None
    await cli.search("src", property="path")
    assert "property=path" in seen[-1]
    await cli.close()


@pytest.mark.asyncio
async def test_compute_metrics_and_its_job() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.headers.get("prefer") == "respond-async":
            return httpx.Response(202, json={
                "id": "3f1c1a52-6a0e-4c8e-9a51-6c9a8f0b2d11", "kind": "compute_metrics",
                "tenant": "demo", "status": "queued", "params": {}, "result": None,
                "error": None, "consumer": "ada", "cancel_requested": False,
                "created_at": "2026-10-08T00:00:00Z", "started_at": None, "finished_at": None,
            })
        return httpx.Response(200, json={
            "tenant": "demo", "metrics": ["pagerank"], "properties": ["graphos_pagerank"],
            "n_nodes": 8, "n_edges_scanned": 6, "n_changed": 8, "n_cleared": 0,
            "edge_stream_ms": 1, "compute_ms": 2, "writeback_ms": 3, "elapsed_ms": 6,
            "dry_run": False,
        })

    cli = GraphosClient(base_url="http://x", token="tok", tenant="demo")
    cli._http = httpx.AsyncClient(base_url="http://x", transport=_mock_transport(handler))
    result = await cli.compute_metrics(metrics=["pagerank"], rel_types=["CALLS"])
    assert (result.n_changed, result.properties) == (8, ["graphos_pagerank"])
    sent = json.loads(captured[0].content)
    assert captured[0].url.path == "/v1/demo/graph/metrics:compute"
    assert sent == {"metrics": ["pagerank"], "damping": 0.85, "dry_run": False,
                    "rel_types": ["CALLS"]}
    job = await cli.submit_compute_metrics()
    assert job.kind == "compute_metrics"
    assert json.loads(captured[1].content)["metrics"] == ["degree", "pagerank", "components"]
    await cli.close()


# ── signing in ────────────────────────────────────────────────────────────────


class _Server:
    """A graphos that signs in and checks tokens; ``expires_in`` and the
    tokens it still accepts are under the test's control."""

    def __init__(self, expires_in: int = 900) -> None:
        self.expires_in = expires_in
        self.logins = 0
        self.valid: set[str] = set()
        self.seen: list[tuple[str, str | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/auth/login"):     # under the base path
            body = json.loads(request.content)
            if body != {"user": "svc", "password": "s3cret-value"}:
                return httpx.Response(401, json={"detail": "wrong user name or password"})
            self.logins += 1
            token = f"t{self.logins}"
            self.valid.add(token)
            return httpx.Response(200, json={"access_token": token, "token_type": "Bearer",
                                             "expires_in": self.expires_in, "refresh_token": None,
                                             "must_change": False})
        auth = request.headers.get("authorization", "")
        self.seen.append((request.url.path, auth))
        if auth.removeprefix("Bearer ") not in self.valid:
            return httpx.Response(401, json={"detail": "invalid or expired token"})
        return httpx.Response(200, json={"consumer": "svc", "admin": False})


@pytest.mark.asyncio
async def test_password_auth_signs_in_once_and_reuses_the_token() -> None:
    server = _Server()
    cli = GraphosClient(base_url="http://x/api", tenant="demo",
                        auth=PasswordAuth("svc", "s3cret-value"), transport=_mock_transport(server))
    await asyncio.gather(*(cli.me() for _ in range(5)))
    assert server.logins == 1                                   # concurrent calls wait for one
    assert {auth for _, auth in server.seen} == {"Bearer t1"}
    await cli.close()


@pytest.mark.asyncio
async def test_password_auth_renews_before_expiry_and_after_a_401() -> None:
    server = _Server(expires_in=0)                              # stale at once: renew each time
    auth = PasswordAuth("svc", "s3cret-value")
    cli = GraphosClient(base_url="http://x", tenant="demo", auth=auth,
                        transport=_mock_transport(server))
    await cli.me()
    await cli.me()
    assert server.logins == 2
    server.expires_in = 900
    await cli.me()                                              # t3, kept
    server.valid.clear()                                        # revoked on the server
    me = await cli.me()                                         # 401, sign in, sent again
    assert me.consumer == "svc" and server.logins == 4
    assert server.seen[-2:] == [("/v1/me", "Bearer t3"), ("/v1/me", "Bearer t4")]
    assert "s3cret" not in repr(auth)
    await cli.close()


@pytest.mark.asyncio
async def test_a_failed_sign_in_raises_graphos_error() -> None:
    cli = GraphosClient(base_url="http://x", tenant="demo",
                        auth=PasswordAuth("svc", "wrong"), transport=_mock_transport(_Server()))
    with pytest.raises(GraphosError) as caught:
        await cli.me()
    assert caught.value.status_code == 401 and "sign-in failed" in caught.value.detail
    await cli.close()


@pytest.mark.asyncio
async def test_credentials_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAPHOS_USER", "svc")
    monkeypatch.setenv("GRAPHOS_PASSWORD", "s3cret-value")
    server = _Server()
    cli = GraphosClient(base_url="http://x", tenant="demo", transport=_mock_transport(server))
    assert (await cli.me()).consumer == "svc" and server.logins == 1
    await cli.close()
    # A token passed explicitly wins over the environment.
    fixed = GraphosClient(base_url="http://x", tenant="demo", token="t1",
                          transport=_mock_transport(server))
    await fixed.me()
    assert server.logins == 1 and server.seen[-1][1] == "Bearer t1"
    await fixed.close()
    with pytest.raises(ValueError, match="either token or auth"):
        GraphosClient(base_url="http://x", tenant="demo", token="t",
                      auth=PasswordAuth("svc", "s3cret-value"))


@pytest.mark.asyncio
async def test_bearer_auth_gets_its_tokens_from_the_program() -> None:
    issued: list[str] = []

    async def source() -> tuple[str, float]:
        issued.append(f"idp{len(issued) + 1}")
        return issued[-1], 300

    server = _Server()
    server.valid.add("idp1")
    cli = GraphosClient(base_url="http://x", tenant="demo", auth=BearerAuth(source),
                        transport=_mock_transport(server))
    await cli.me()
    await cli.me()
    assert issued == ["idp1"]                                   # reused while valid
    server.valid = {"idp2"}                                     # the old one was revoked
    await cli.me()                                              # 401: asks for a new one
    assert issued == ["idp1", "idp2"] and server.seen[-1][1] == "Bearer idp2"
    await cli.close()


@pytest.mark.asyncio
async def test_export_and_import_a_schema() -> None:
    captured: list[httpx.Request] = []
    doc = {
        "_export": {"format": "graphos.schema", "version": 1},
        "name": "demo", "vertex_labels": ["A", "B"],
        "edge_endpoints": {"R": [{"src": "A", "dst": "B"}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "GET":
            assert request.url.path == "/v1/tenants/demo/spec"
            return httpx.Response(200, json=doc)
        return httpx.Response(200, json={"tenant": "x", "status": "bootstrapped"})

    cli = GraphosClient(base_url="http://x", token="tok", tenant="demo")
    cli._http = httpx.AsyncClient(base_url="http://x", transport=_mock_transport(handler))
    assert await cli.export_schema() == doc
    await cli.import_schema(doc, name="copy", replace=True)
    sent = json.loads(captured[1].content)
    assert sent["name"] == "copy" and sent["_export"] == doc["_export"]
    assert sent["edge_endpoints"] == doc["edge_endpoints"]
    assert dict(captured[1].url.params) == {"force_shrink": "true"}
    await cli.import_schema(doc, dry_run=True)
    assert dict(captured[2].url.params) == {"dry_run": "true"}
    assert json.loads(captured[2].content)["name"] == "demo"
    await cli.close()


@pytest.mark.asyncio
async def test_import_splits_more_than_5000_pairs_over_several_calls() -> None:
    bodies: list[tuple[dict, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((json.loads(request.content), dict(request.url.params)))
        return httpx.Response(200, json={"tenant": "demo", "warnings": []})

    pairs = [{"src": f"L{i}", "dst": "B"} for i in range(12_001)]
    doc = {"name": "demo", "vertex_labels": ["B"], "edge_endpoints": {"R": pairs[:7000],
                                                                       "S": pairs[7000:]}}
    cli = GraphosClient(base_url="http://x", token="tok", tenant="demo")
    cli._http = httpx.AsyncClient(base_url="http://x", transport=_mock_transport(handler))
    await cli.import_schema(doc, replace=True)
    assert [sum(len(v) for v in b["edge_endpoints"].values()) for b, _ in bodies] == [
        5000, 5000, 2001,
    ]
    # The first call carries the rest of the schema and replaces; the others add pairs.
    assert bodies[0][0]["vertex_labels"] == ["B"] and bodies[0][1] == {"force_shrink": "true"}
    assert all(set(b) == {"name", "edge_endpoints"} and p == {} for b, p in bodies[1:])
    sent = [p for b, _ in bodies for ps in b["edge_endpoints"].values() for p in ps]
    assert sent == pairs

    bodies.clear()
    preview = await cli.import_schema(doc, dry_run=True)
    assert len(bodies) == 1 and "7001 more declared label pairs" in preview["warnings"][0]
    await cli.close()


_HEADER = {"_kind": "header", "format": "graphos.snapshot", "version": 1, "tenant": "demo",
           "schema": {"name": "demo", "vertex_labels": ["Doc"]}}
_FILE = "".join(json.dumps(line) + "\n" for line in (
    _HEADER,
    {"_kind": "node", "id": "d1", "label": "Doc", "properties": {}},
    {"_kind": "end", "counts": {"nodes": 1, "edges": 0}},
)).encode()


@pytest.mark.asyncio
async def test_export_dataset_streams_to_a_file(tmp_path) -> None:  # noqa: ANN001
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_FILE, headers={"content-type": "application/gzip"})

    cli = GraphosClient(base_url="http://x", token="tok", tenant="demo")
    cli._http = httpx.AsyncClient(base_url="http://x", transport=_mock_transport(handler))
    out = tmp_path / "demo.ndjson.gz"
    assert await cli.export_dataset(out, labels=["Doc"]) == {"bytes": len(_FILE)}
    assert out.read_bytes() == _FILE                      # written as received
    assert seen[0].url.path == "/v1/demo/graph/snapshot"
    assert dict(seen[0].url.params) == {"computed": "true", "gzip": "true", "labels": "Doc"}
    await cli.close()


@pytest.mark.asyncio
async def test_import_dataset_applies_the_schema_then_streams_the_file(tmp_path) -> None:  # noqa: ANN001
    import gzip  # noqa: PLC0415

    seen: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.read()))
        if request.url.path == "/v1/me":
            return httpx.Response(200, json={"consumer": "ada"})
        if request.url.path == "/v1/tenants":
            return httpx.Response(200, json={"tenant": "copy"})
        return httpx.Response(200, json={"nodes_upserted": 1, "complete": True})

    cli = GraphosClient(base_url="http://x", token="tok", tenant="demo")
    cli._http = httpx.AsyncClient(base_url="http://x", transport=_mock_transport(handler))
    path = tmp_path / "demo.ndjson.gz"
    path.write_bytes(gzip.compress(_FILE))
    from graphos_client import read_dataset_header  # noqa: PLC0415
    assert read_dataset_header(path) == _HEADER
    result = await cli.import_dataset(path, mode="empty", schema="apply", name="copy")
    assert result == {"nodes_upserted": 1, "complete": True}
    schema_call, me_call, upload = seen
    assert schema_call[1] == "http://x/v1/tenants"
    assert json.loads(schema_call[2]) == {"name": "copy", "vertex_labels": ["Doc"]}
    assert me_call[1] == "http://x/v1/me"
    assert upload[1] == "http://x/v1/copy/graph/snapshot-import?mode=empty"
    assert upload[2] == path.read_bytes()                 # the file as it is, gzip included
    await cli.close()



@pytest.mark.asyncio
async def test_unregister_restore_and_delete_a_tenant_with_its_data() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"tenants": [], "unregistered": [
                {"name": "old", "kind": "unregistered", "vertex_labels": 3, "edge_labels": 1,
                 "unregistered_at": "2026-10-14T08:00:00Z", "unregistered_by": "olive"},
                {"name": "older", "kind": "orphan"},
            ]})
        return httpx.Response(200, json={"tenant": "demo", "status": "ok"})

    cli = GraphosClient(base_url="http://x", token="tok", tenant="demo")
    cli._http = httpx.AsyncClient(base_url="http://x", transport=_mock_transport(handler))
    await cli.unregister_tenant()
    await cli.restore_tenant("old")
    await cli.unregister_tenant("old", drop_data=True)
    listed = await cli.list_unregistered_tenants()
    assert [(r.method, r.url.path, dict(r.url.params)) for r in captured] == [
        ("DELETE", "/v1/tenants/demo", {}),
        ("POST", "/v1/tenants/old/restore", {}),
        ("DELETE", "/v1/tenants/old", {"drop_data": "true"}),
        ("GET", "/v1/tenants", {"unregistered": "true"}),
    ]
    assert [(u.name, u.kind, u.vertex_labels) for u in listed] == [
        ("old", "unregistered", 3), ("older", "orphan", 0),
    ]
    assert listed[0].unregistered_by == "olive"
    await cli.close()


@pytest.mark.asyncio
async def test_a_meta_key_given_as_none_is_sent_as_null() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"tenant": "demo"})

    cli = GraphosClient(base_url="http://x", token="tok", tenant="demo")
    cli._http = httpx.AsyncClient(base_url="http://x", transport=_mock_transport(handler))
    await cli.register_tenant(meta={"draft": None, "owner": "team-a"})
    assert json.loads(captured[0].content)["meta"] == {"draft": None, "owner": "team-a"}
    await cli.close()

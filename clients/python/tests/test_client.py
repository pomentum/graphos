"""Client-side tests against a mocked HTTP transport.

We use httpx's ``MockTransport`` to intercept requests and assert that the
client builds them correctly (URL, headers, body shape) and parses
responses into the right Pydantic types. No real graphos required.
"""

from __future__ import annotations

import json

import httpx
import pytest

from graphos_client import (
    EdgeUpsert,
    GraphosClient,
    GraphosError,
    NodeUpsert,
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

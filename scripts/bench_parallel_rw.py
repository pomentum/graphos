"""Parallel writer + readers benchmark.

Spawns one writer task that streams ~50k node + edge upserts via the
:bulk endpoints, and N reader tasks that run a rotating
mix of graph queries against the same tenant.

Measures:
  - writer:  total wall clock, server-side time, rows/s
  - readers: count of queries, p50/p95/p99 latency, error count
  - read latency degradation while writer is active

The goal is to verify that the COPY-staged bulk-write path doesn't
starve concurrent reads, and to measure read-side impact of a
high-volume populate.

Usage:
    GRAPHOS_USER=<user> GRAPHOS_PASSWORD=<password> \\
        python bench_parallel_rw.py http://localhost:8080 BENCH \\
        --nodes 13000 --edges 44000 \\
        --batch 5000 --readers 8 --read-duration 60

Signs in as GRAPHOS_USER (write on the tenant) and signs in again before
the 15-minute access token runs out.

If --read-duration outlives the writer (typical), readers continue
solo so we can compare "writer-active" vs "writer-done" latency.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import time
from typing import Any

import httpx

# ── Synthetic data generators ────────────────────────────────────────


def _gen_nodes_ndjson(n: int) -> str:
    """One NDJSON line per node, all in :Node label."""
    return "\n".join(
        json.dumps({
            "label": "Node",
            "id": f"n{i:06d}",
            "properties": {
                "name": f"name_{i}",
                "value": i,
                "tag": f"tag_{i % 100}",
                "bucket": i % 20,
            },
        })
        for i in range(n)
    )


def _gen_edges_ndjson(n_nodes: int, n_edges: int) -> str:
    return "\n".join(
        json.dumps({
            "type": "EDGE",
            "src_label": "Node",
            "dst_label": "Node",
            "src_id": f"n{i % n_nodes:06d}",
            "dst_id": f"n{(i * 7 + 1) % n_nodes:06d}"
            if (i * 7 + 1) % n_nodes != i % n_nodes
            else f"n{((i * 7 + 1) % n_nodes + 1) % n_nodes:06d}",
            "properties": {"weight": i % 10, "kind": f"kind_{i % 5}"},
        })
        for i in range(n_edges)
    )


# ── Reader queries ────────────────────────────────────────────────────
#
# Each entry is a callable that picks a random query body each
# iteration so the reader can't be served entirely from prepared-
# statement cache.

# Each query function returns (name, method, url_suffix, body_or_None).
# Body is json-serialisable; url is relative to /v1/{tenant}/graph.

def _q_neighbors(rng: random.Random, n_nodes: int) -> tuple[str, str, str, Any]:
    nid = f"n{rng.randrange(n_nodes):06d}"
    return (
        "neighbors_1hop",
        "GET",
        f"/neighbors?node_id={nid}&rel_types=EDGE&limit=50",
        None,
    )


def _q_expand_2(rng: random.Random, n_nodes: int) -> tuple[str, str, str, Any]:
    nid = f"n{rng.randrange(n_nodes):06d}"
    return (
        "expand_2hop",
        "GET",
        f"/expand?node_id={nid}&rel_types=EDGE&max_hops=2&direction=out&limit=100",
        None,
    )


def _q_stats(rng: random.Random, n_nodes: int) -> tuple[str, str, str, Any]:
    del rng, n_nodes
    return ("stats", "GET", "/stats", None)


def _q_induced(rng: random.Random, n_nodes: int) -> tuple[str, str, str, Any]:
    ids = [f"n{rng.randrange(n_nodes):06d}" for _ in range(20)]
    return (
        "induced_edges",
        "POST",
        "/induced_edges",
        {"node_ids": ids, "rel_types": ["EDGE"]},
    )


_READ_QUERIES = [_q_neighbors, _q_expand_2, _q_stats, _q_induced]


# ── Helpers ───────────────────────────────────────────────────────────


def _quantile(samples: list[float], q: float) -> float:
    if not samples:
        return 0.0
    if len(samples) == 1:
        return samples[0]
    return statistics.quantiles(samples, n=100)[int(q * 100) - 1]


# ── Tasks ─────────────────────────────────────────────────────────────


async def writer_task(
    client: httpx.AsyncClient,
    base: str,
    tenant: str,
    n_nodes: int,
    n_edges: int,
    batch_size: int,
    metrics: dict[str, Any],
) -> None:
    """Stream nodes then edges to the tenant in batch_size chunks."""
    nodes_url = f"{base}/v1/{tenant}/graph/nodes:bulk"
    edges_url = f"{base}/v1/{tenant}/graph/edges:bulk"
    headers = {"Content-Type": "application/x-ndjson"}

    # nodes ─────────
    nodes = _gen_nodes_ndjson(n_nodes).split("\n")
    node_started = time.perf_counter()
    server_node_ms = 0
    for off in range(0, len(nodes), batch_size):
        body = "\n".join(nodes[off : off + batch_size])
        r = await client.post(nodes_url, content=body, headers=headers, timeout=180.0)
        r.raise_for_status()
        server_node_ms += r.json().get("elapsed_ms", 0)
    node_wall = time.perf_counter() - node_started

    # edges ─────────
    edges = _gen_edges_ndjson(n_nodes, n_edges).split("\n")
    edge_started = time.perf_counter()
    server_edge_ms = 0
    for off in range(0, len(edges), batch_size):
        body = "\n".join(edges[off : off + batch_size])
        r = await client.post(edges_url, content=body, headers=headers, timeout=180.0)
        r.raise_for_status()
        server_edge_ms += r.json().get("elapsed_ms", 0)
    edge_wall = time.perf_counter() - edge_started

    metrics["writer"] = {
        "nodes_wall_s": round(node_wall, 2),
        "nodes_server_s": round(server_node_ms / 1000.0, 2),
        "nodes_per_s": round(n_nodes / max(node_wall, 1e-6), 0),
        "edges_wall_s": round(edge_wall, 2),
        "edges_server_s": round(server_edge_ms / 1000.0, 2),
        "edges_per_s": round(n_edges / max(edge_wall, 1e-6), 0),
        "total_wall_s": round(node_wall + edge_wall, 2),
        "total_rows": n_nodes + n_edges,
    }
    metrics["writer_done_at"] = time.monotonic()


async def reader_task(
    client: httpx.AsyncClient,
    base: str,
    tenant: str,
    n_nodes: int,
    duration_s: float,
    reader_id: int,
    metrics: dict[str, Any],
) -> None:
    """Run rotating queries against the tenant for duration_s seconds.

    Each query timestamp is captured so we can split metrics into
    ``writer_active`` (started before writer finished) and
    ``writer_done`` windows.
    """
    rng = random.Random(reader_id * 0x9E3779B9)
    deadline = time.monotonic() + duration_s

    samples_active: list[float] = []
    samples_done: list[float] = []
    by_query_active: dict[str, list[float]] = {}
    by_query_done: dict[str, list[float]] = {}
    errors = 0

    base_graph_url = f"{base}/v1/{tenant}/graph"
    while time.monotonic() < deadline:
        name, method, suffix, body = _READ_QUERIES[
            rng.randrange(len(_READ_QUERIES))
        ](rng, n_nodes)
        url = f"{base_graph_url}{suffix}"
        t0 = time.perf_counter()
        try:
            if method == "GET":
                r = await client.get(url, timeout=30.0)
            else:
                r = await client.post(url, json=body, timeout=30.0)
            r.raise_for_status()
        except Exception:
            errors += 1
            continue
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        writer_active = "writer_done_at" not in metrics or (
            t0 < metrics["writer_done_at"]
        )
        if writer_active:
            samples_active.append(elapsed_ms)
            by_query_active.setdefault(name, []).append(elapsed_ms)
        else:
            samples_done.append(elapsed_ms)
            by_query_done.setdefault(name, []).append(elapsed_ms)

    metrics.setdefault("readers", []).append({
        "reader_id": reader_id,
        "n_active": len(samples_active),
        "n_done": len(samples_done),
        "errors": errors,
        "active_p50_ms": round(_quantile(sorted(samples_active), 0.50), 1),
        "active_p95_ms": round(_quantile(sorted(samples_active), 0.95), 1),
        "active_p99_ms": round(_quantile(sorted(samples_active), 0.99), 1),
        "done_p50_ms": round(_quantile(sorted(samples_done), 0.50), 1) if samples_done else 0.0,
        "done_p95_ms": round(_quantile(sorted(samples_done), 0.95), 1) if samples_done else 0.0,
        "done_p99_ms": round(_quantile(sorted(samples_done), 0.99), 1) if samples_done else 0.0,
        "by_query_active_p50": {
            name: round(_quantile(sorted(s), 0.50), 1)
            for name, s in by_query_active.items()
        },
        "by_query_done_p50": {
            name: round(_quantile(sorted(s), 0.50), 1)
            for name, s in by_query_done.items()
        },
    })


# ── Entrypoint ────────────────────────────────────────────────────────


class _SignIn(httpx.Auth):
    """Bearer token for GRAPHOS_USER / GRAPHOS_PASSWORD, renewed after 10 minutes
    (access tokens last 15)."""

    renew_s = 600.0

    def __init__(self, base: str) -> None:
        self._base = base
        self._token = ""
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def async_auth_flow(self, request: httpx.Request):
        async with self._lock:
            if not self._token or time.monotonic() - self._at > self.renew_s:
                login = yield httpx.Request(
                    "POST", f"{self._base}/v1/auth/login",
                    json={"user": os.environ["GRAPHOS_USER"],
                          "password": os.environ["GRAPHOS_PASSWORD"]},
                )
                await login.aread()
                login.raise_for_status()
                self._token, self._at = login.json()["access_token"], time.monotonic()
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


async def main_async(args: argparse.Namespace) -> None:
    metrics: dict[str, Any] = {}
    async with httpx.AsyncClient(auth=_SignIn(args.base)) as client:
        tasks = [
            asyncio.create_task(
                writer_task(
                    client, args.base, args.tenant,
                    args.nodes, args.edges, args.batch, metrics,
                ),
                name="writer",
            ),
        ]
        for i in range(args.readers):
            tasks.append(
                asyncio.create_task(
                    reader_task(
                        client, args.base, args.tenant,
                        args.nodes, args.read_duration, i, metrics,
                    ),
                    name=f"reader-{i}",
                )
            )
        await asyncio.gather(*tasks)

    print(json.dumps(metrics, indent=2, default=str))

    # Aggregate readers
    if "readers" in metrics:
        all_active = [
            ms for r in metrics["readers"]
            for ms in [r["active_p50_ms"]] if r["n_active"]
        ]
        all_done = [
            ms for r in metrics["readers"]
            for ms in [r["done_p50_ms"]] if r["n_done"]
        ]
        print("\n── reader aggregate ─────────────────────────")
        print(
            f"  active window p50 across readers: "
            f"{round(statistics.median(all_active), 1) if all_active else 'n/a'} ms"
        )
        print(
            f"  done   window p50 across readers: "
            f"{round(statistics.median(all_done), 1) if all_done else 'n/a'} ms"
        )
    if "writer" in metrics:
        w = metrics["writer"]
        print("\n── writer summary ────────────────────────────")
        print(f"  total wall: {w['total_wall_s']} s "
              f"({w['total_rows']} rows, "
              f"{round(w['total_rows'] / w['total_wall_s'], 0)} rows/s)")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("base", nargs="?", default="http://localhost:8080")
    p.add_argument("tenant", nargs="?", default="BENCH")
    p.add_argument("--nodes", type=int, default=13_000)
    p.add_argument("--edges", type=int, default=44_000)
    p.add_argument("--batch", type=int, default=5000)
    p.add_argument("--readers", type=int, default=8)
    p.add_argument("--read-duration", type=float, default=60.0)
    args = p.parse_args()
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

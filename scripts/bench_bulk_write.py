"""Benchmark: bulk write throughput of graphos's COPY-staged SQL upserts.

Streams synthetic nodes, then edges, to the ``:bulk`` endpoints in batches
and reports client- and server-side rates.

Every graphos write goes through this path.  The per-row Cypher ``MERGE``
baseline quoted in docs/guide/apache-age.md (31 edges/s) was measured with an
earlier version that still had a Cypher write path; it can no longer be
reproduced with this script.

Usage:
    GRAPHOS_USER=<user> GRAPHOS_PASSWORD=<password> \
        python bench_bulk_write.py http://localhost:8080 bench

Signs in as GRAPHOS_USER (a user or service account with write on the
tenant) and signs in again before the 15-minute access token runs out.
Assumes the tenant is registered with vertex_labels=['Node'] and
edge_labels=['EDGE'].
"""
from __future__ import annotations

import json
import os
import sys
import time
from urllib.request import Request, urlopen

# Access tokens last 15 minutes; sign in again after 10.
_RENEW_S = 600.0
_session: dict[str, object] = {"token": "", "at": 0.0}


def _token(base: str) -> str:
    """An access token for GRAPHOS_USER / GRAPHOS_PASSWORD, renewed when old."""
    if not _session["token"] or time.monotonic() - float(_session["at"]) > _RENEW_S:  # type: ignore[arg-type]
        body = json.dumps({
            "user": os.environ["GRAPHOS_USER"], "password": os.environ["GRAPHOS_PASSWORD"],
        }).encode("utf-8")
        req = Request(f"{base}/v1/auth/login", data=body, method="POST",
                      headers={"Content-Type": "application/json"})
        with urlopen(req) as resp:
            _session["token"] = json.loads(resp.read().decode("utf-8"))["access_token"]
        _session["at"] = time.monotonic()
    return str(_session["token"])


def _post_ndjson(url: str, lines: list[str], token: str) -> tuple[int, dict]:
    body = "\n".join(lines).encode("utf-8")
    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-ndjson",
            "Authorization": f"Bearer {token}",
        },
    )
    started = time.perf_counter()
    with urlopen(req) as resp:
        status = resp.status
        payload = json.loads(resp.read().decode("utf-8"))
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    payload["_client_elapsed_ms"] = elapsed_ms
    return status, payload


def _gen_nodes(n: int) -> list[str]:
    """Return NDJSON node lines."""
    return [
        json.dumps({
            "label": "Node",
            "id": f"n{i:06d}",
            "properties": {
                "name": f"name_{i}",
                "value": i,
                "tag": "synth",
            },
        })
        for i in range(n)
    ]


def _gen_edges(n_nodes: int, n_edges: int) -> list[str]:
    """Return NDJSON edge lines pointing at existing synthetic nodes."""
    out = []
    for i in range(n_edges):
        src = i % n_nodes
        dst = (i * 7 + 1) % n_nodes
        if src == dst:
            dst = (dst + 1) % n_nodes
        out.append(json.dumps({
            "type": "EDGE",
            "src_label": "Node",
            "dst_label": "Node",
            "src_id": f"n{src:06d}",
            "dst_id": f"n{dst:06d}",
            "properties": {"weight": (i % 10), "kind": "synth"},
        }))
    return out


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
    tenant = sys.argv[2] if len(sys.argv) > 2 else "bench"
    n_nodes = int(sys.argv[3]) if len(sys.argv) > 3 else 13_000
    n_edges = int(sys.argv[4]) if len(sys.argv) > 4 else 44_000
    batch_size = int(sys.argv[5]) if len(sys.argv) > 5 else 1_000

    nodes_url = f"{base}/v1/{tenant}/graph/nodes:bulk"
    edges_url = f"{base}/v1/{tenant}/graph/edges:bulk"

    print(f"==> generating {n_nodes} nodes + {n_edges} edges (batch={batch_size})")
    nodes = _gen_nodes(n_nodes)
    edges = _gen_edges(n_nodes, n_edges)

    print("==> POSTing nodes:")
    node_started = time.perf_counter()
    server_node_ms = 0
    for off in range(0, len(nodes), batch_size):
        status, payload = _post_ndjson(nodes_url, nodes[off : off + batch_size], _token(base))
        server_node_ms += payload.get("elapsed_ms", 0)
        if (off // batch_size) % 5 == 0:
            print(
                f"   batch {off:>6}-{off+batch_size:<6}  "
                f"HTTP {status}  server={payload.get('elapsed_ms', '-')}ms  "
                f"upserted={payload.get('nodes_upserted', '-')}"
            )
    node_total_s = time.perf_counter() - node_started
    node_rps = n_nodes / node_total_s if node_total_s > 0 else 0
    print(
        f"   NODES total: client={node_total_s:.2f}s  "
        f"server={server_node_ms / 1000:.2f}s  "
        f"rate={node_rps:.0f} nodes/s"
    )

    print("==> POSTing edges:")
    edge_started = time.perf_counter()
    server_edge_ms = 0
    for off in range(0, len(edges), batch_size):
        status, payload = _post_ndjson(edges_url, edges[off : off + batch_size], _token(base))
        server_edge_ms += payload.get("elapsed_ms", 0)
        if (off // batch_size) % 5 == 0:
            print(
                f"   batch {off:>6}-{off+batch_size:<6}  "
                f"HTTP {status}  server={payload.get('elapsed_ms', '-')}ms  "
                f"upserted={payload.get('edges_upserted', '-')}  "
                f"skipped={payload.get('edges_skipped', '-')}"
            )
    edge_total_s = time.perf_counter() - edge_started
    edge_rps = n_edges / edge_total_s if edge_total_s > 0 else 0
    print(
        f"   EDGES total: client={edge_total_s:.2f}s  "
        f"server={server_edge_ms / 1000:.2f}s  "
        f"rate={edge_rps:.0f} edges/s"
    )

    grand_total = node_total_s + edge_total_s
    print(
        f"\n==> GRAND TOTAL: {grand_total:.1f}s "
        f"({n_nodes + n_edges} rows, {(n_nodes + n_edges) / grand_total:.0f}/s combined)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

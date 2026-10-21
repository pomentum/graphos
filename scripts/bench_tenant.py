"""Measure one tenant of a running graphos: how fast its reads answer, alone
and under concurrent users, and, on request, how long its graph jobs take.

Point it at any graphos and any tenant. It learns what to read from the
tenant itself (the schema summary, a sample of nodes): no dataset, schema
or query has to be written for it.

    GRAPHOS_USER=<user> GRAPHOS_PASSWORD=<password> \\
        python scripts/bench_tenant.py http://localhost:8080 <tenant>

What it measures:

- **Reads, one user:** each read of the mix `--runs` times after
  `--warmup` unrecorded runs: a node by id, a filtered scan, text search
  (with hits and without), neighbours, expand 1-3 hops, shortest paths,
  the graph map, an aggregation, the top nodes by PageRank, tenant
  statistics. A read the tenant cannot answer (no search index, no metrics,
  no property with few values) is skipped and the summary says why.
- **Saved queries:** each of the tenant's saved queries, the same way.
  Queries with parameters run only with values from `--saved-params`.
- **Concurrent users:** `--users` steps of `--step-seconds` each; every
  user runs reads of the mix in a random order, with `--think-ms` between
  them. Latency percentiles, errors and throughput per step.
- **Cold reads** (`--cold-cmd`, `--cold-runs`): before each cold run the
  command you give is run (it should restart PostgreSQL and graphos-api and
  drop the page cache, on your setup), then graphos is awaited ready.
- **Resources** (`--docker <container>`, repeatable): CPU and memory of the
  containers named, sampled with `docker stats` during the concurrency steps.
- **Graph jobs** (`--jobs`): the graph metrics and the community recompute,
  each run twice (a run, then a re-run of the same graph). They write
  `graphos_*` and `community_id` onto the tenant's nodes, as these jobs
  always do, and need `maintain` on the tenant.

Everything else only reads, with a user that has `read` on the tenant. Run
it against the graphos you want measured, at a time when its figures mean
something to you: other load on the same machine shows up in them.

Output, in `--out` (default `bench-<tenant>-<UTC time>`): `environment.csv`,
`reads.csv`, `saved_queries.csv`, `concurrency.csv`, `resources.csv`,
`jobs.csv`, `raw.jsonl` (every measurement, so every figure can be
recomputed) and `summary.md`. Statistics only: read names, counts and
timings, never a label, property, id or value of the data, unless
`--show-names`. p95 is the 95th percentile with linear interpolation
between ranks (`statistics.quantiles`, `method="inclusive"`).

Signs in with GRAPHOS_USER and GRAPHOS_PASSWORD (renewed as needed), or uses
GRAPHOS_TOKEN as is.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import re
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from graphos_client import GraphosClient, GraphosError, PasswordAuth

NO_HITS = "qzxwvjkq"          # a word no text index holds
Read = Callable[[GraphosClient], Awaitable[int]]   # one read; the rows it answered


# ── Output ───────────────────────────────────────────────────────────


class Out:
    """The output folder: raw measurements as they come, tables at the end."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self._raw = (folder / "raw.jsonl").open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def record(self, kind: str, **row: Any) -> None:
        with self._lock:
            self._raw.write(json.dumps({"kind": kind, "at": now(), **row}) + "\n")
            self._raw.flush()

    def table(self, name: str, header: list[str], rows: list[list[Any]]) -> None:
        with (self.folder / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    def close(self) -> None:
        self._raw.close()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def p95(values: list[float]) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[94]


def pct(values: list[float], q: int) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def ms(values: list[float]) -> list[Any]:
    """median, p95, min, max, rounded to 0.1 ms."""
    if not values:
        return ["", "", "", ""]
    return [round(statistics.median(values), 1), round(p95(values), 1),
            round(min(values), 1), round(max(values), 1)]


# ── What to read: learned from the tenant ────────────────────────────


@dataclass
class Plan:
    reads: dict[str, Read] = field(default_factory=dict)
    saved: dict[str, Read] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)   # read -> why
    facts: dict[str, Any] = field(default_factory=dict)     # environment rows
    names: dict[str, str] = field(default_factory=dict)     # shown with --show-names only


def _value(v: Any) -> Any:
    """A schema summary value (text) as the JSON value it stands for."""
    if v.kind in ("number", "boolean"):
        return json.loads(v.value)
    return v.value


async def _start_node(cli: GraphosClient, label: str, sample: int) -> tuple[str, int] | None:
    """An ordinary node of ``label``: among a sample, the one with the median
    number of neighbours (hubs make every read slow, leaves make it trivial)."""
    scan = await cli.scan_nodes(labels=[label], limit=sample)
    degrees = []
    for item in scan.items:
        n = len((await cli.neighbors(item.id, limit=500)).neighbors)
        if n:
            degrees.append((n, item.id))
    if not degrees:
        return None
    degrees.sort()
    n, node = degrees[len(degrees) // 2]
    return node, n


async def _search_term(cli: GraphosClient, indexes: list[Any]) -> tuple[str, str, str] | None:
    """Something a search index holds, from stored values: (index, term, kind).
    A word of 4 letters or more where the values have words; otherwise a whole
    value (an identifier, a code)."""
    whole = None
    for idx in indexes:
        for label in idx.labels:
            for prop in idx.properties:
                scan = await cli.scan_nodes(labels=[label], project=[prop], limit=50)
                values = sorted(str(v) for v in (i.properties.get(prop) for i in scan.items) if v)
                words = sorted(w for v in values for w in re.findall(r"[^\W\d_]{4,}", v))
                if words:
                    return idx.name, words[len(words) // 2], "word"
                if values and whole is None:
                    whole = (idx.name, values[len(values) // 2], "whole value")
    return whole


async def discover(cli: GraphosClient, args: argparse.Namespace) -> Plan:
    plan = Plan()
    info = await cli.info()
    summary = await cli.schema_summary()
    stats = await cli.tenant_stats()
    plan.facts.update({
        "graphos version": info.get("version"), "postgres version": info.get("postgres_version"),
        "age version": info.get("age_version"), "nodes": stats.n_vertices, "edges": stats.n_edges,
        "labels": summary.labels_total, "edge types": len({lk.type for lk in summary.links}),
        "search indexes": len(summary.search_indexes), "saved queries": len(summary.saved_queries),
        "computed properties": ", ".join(summary.computed) or "none",
    })

    linked = {lk.src_label for lk in summary.links if lk.count} | {
        lk.dst_label for lk in summary.links if lk.count}
    by_count = sorted((lb for lb in summary.labels if lb.label in linked),
                      key=lambda lb: -lb.count)
    label = args.start_label or (by_count[0].label if by_count else None)
    start = None
    if args.start_id and label:
        start = (args.start_id, len((await cli.neighbors(args.start_id, limit=500)).neighbors))
    elif label:
        start = await _start_node(cli, label, args.sample)
    if start:
        node, degree = start
        plan.facts["start node's neighbours"] = degree
        plan.names["start"] = f"{label} {node}"
        plan.reads["node_by_id"] = lambda c: _count(c.get_node(node, label=label), 1)
        plan.reads["neighbours"] = lambda c: _len(c.neighbors(node, limit=100), "neighbors")
        for hops in (1, 2, 3):
            plan.reads[f"expand_{hops}_hop{'s' if hops > 1 else ''}"] = (
                lambda c, h=hops: _len(c.expand(node, max_hops=h, limit=1000), "paths"))
        reach = await cli.expand(node, max_hops=3, limit=1000)
        far = sorted(reach.paths, key=lambda p: (-p.hops, p.end_label, p.end_id))
        if far and far[0].hops >= 2:
            dst, dst_label = far[0].end_id, far[0].end_label
            plan.facts["path length searched (hops)"] = far[0].hops
            plan.names["path end"] = f"{dst_label} {dst}"
            plan.reads["shortest_path"] = lambda c: _len(c.find_paths(
                node, dst, src_label=label, dst_label=dst_label, max_hops=4), "paths")
        else:
            plan.skipped["shortest_path"] = "nothing 2 or more hops from the start node"
    else:
        for name in ("node_by_id", "neighbours", "expand_1_hop", "expand_2_hops",
                     "expand_3_hops", "shortest_path"):
            plan.skipped[name] = "no node with relationships to start from"

    # A property with few values (the summary lists them): a filtered scan and
    # an aggregation over it. The value is one of middling frequency.
    few = [(lb.label, p) for lb in summary.labels for p in lb.properties
           if p.values and len(p.values) >= 2 and not p.name.startswith("graphos_")
           and all(not v.value.endswith("…") and v.kind in ("string", "number", "boolean")
                   for v in p.values)]
    few.sort(key=lambda lp: (-lp[1].records, lp[0], lp[1].name))
    if few:
        lab, prop = few[0]
        value = _value(prop.values[len(prop.values) // 2])
        plan.names["filtered property"] = f"{lab}.{prop.name} = {value!r}"
        plan.reads["scan_100_by_property"] = lambda c: _len(c.scan_nodes(
            labels=[lab], where=[{"property": prop.name, "op": "=", "value": value}], limit=100),
            "items")
        plan.reads["aggregate_one_property"] = lambda c: _len(
            c.aggregate_nodes(label=lab, group_by=prop.name), "groups")
    else:
        plan.skipped["scan_100_by_property"] = "no property with 20 values or fewer"
        plan.skipped["aggregate_one_property"] = "no property with 20 values or fewer"

    if summary.search_indexes:
        first = summary.search_indexes[0].name
        plan.reads["search_0_hits"] = lambda c: _len(
            c.search(NO_HITS, index=first, limit=20), "nodes")
        found = await _search_term(cli, summary.search_indexes)
        if found:
            index, term, kind = found
            plan.facts["search term"] = f"a {kind} from a stored value"
            plan.names["search term"] = f"{index}: {term}"
            plan.reads["search_20_hits"] = lambda c: _len(
                c.search(term, index=index, limit=20), "nodes")
        else:
            plan.skipped["search_20_hits"] = "no stored value found in the indexed properties"
    else:
        plan.skipped["search_0_hits"] = "the tenant has no search index"
        plan.skipped["search_20_hits"] = "the tenant has no search index"

    if "graphos_pagerank" in summary.computed and label:
        plan.reads["top_20_by_pagerank"] = lambda c: _len(c.scan_nodes(
            labels=[label], limit=20, order_by=[{"property": "graphos_pagerank", "desc": True}]),
            "items")
    else:
        plan.skipped["top_20_by_pagerank"] = "PageRank not computed on the tenant"
    plan.reads["graph_map"] = lambda c: _len(c.graph_map(), "links")
    plan.reads["tenant_statistics"] = lambda c: _count(c.tenant_stats(), 1)

    params = (json.loads(Path(args.saved_params).read_text(encoding="utf-8"))
              if args.saved_params else {})
    for i, q in enumerate(sorted(summary.saved_queries, key=lambda q: q.name), 1):
        key = f"saved_{i:02d}"
        plan.names[key] = q.name
        if q.params and q.name not in params:
            plan.skipped[key] = "has parameters; give their values in --saved-params"
            continue
        plan.saved[key] = _saved(q.name, params.get(q.name, {}))
    if args.reads:
        wanted = set(args.reads.split(","))
        plan.reads = {k: v for k, v in plan.reads.items() if k in wanted}
    return plan


def _saved(name: str, params: dict[str, Any]) -> Read:
    return lambda c: _len(c.run_saved_query(name, params), "rows")


async def _len(call: Awaitable[Any], attr: str) -> int:
    return len(getattr(await call, attr))


async def _count(call: Awaitable[Any], n: int) -> int:
    await call
    return n


# ── Measuring ────────────────────────────────────────────────────────


async def timed(cli: GraphosClient, read: Read) -> tuple[float, int | None, str]:
    """Run one read: (ms, rows, status)."""
    t0 = time.perf_counter()
    try:
        rows = await read(cli)
        status = "ok"
    except GraphosError as exc:
        rows, status = None, f"error {exc.status_code}"
    except httpx.HTTPError as exc:
        rows, status = None, f"error {type(exc).__name__}"
    return (time.perf_counter() - t0) * 1000, rows, status


async def one_user(cli: GraphosClient, reads: dict[str, Read], kind: str, runs: int,
                   warmup: int, out: Out) -> None:
    for name, read in reads.items():
        for _ in range(warmup):
            await timed(cli, read)
        for run in range(1, runs + 1):
            took, rows, status = await timed(cli, read)
            out.record(kind, read=name, cache="warm", run=run, ms=round(took, 2), rows=rows,
                       status=status)
        print(f"  {name}: done", file=sys.stderr)


def wait_ready(base: str, timeout: float) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if httpx.get(f"{base}/readyz", timeout=5).status_code == 200:
                return time.monotonic() - t0
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise SystemExit(f"graphos was not ready {timeout:.0f} s after --cold-cmd")


async def cold(make: Callable[[], GraphosClient], reads: dict[str, Read], kind: str,
               args: argparse.Namespace, out: Out) -> None:
    """Each cold run follows the operator's restart command, then one read."""
    for name, read in reads.items():
        for run in range(1, args.cold_runs + 1):
            # The operator's own command, run as given.
            await asyncio.to_thread(subprocess.run, args.cold_cmd, shell=True, check=True)
            ready = await asyncio.to_thread(wait_ready, args.base, args.ready_timeout)
            async with make() as cli:
                took, rows, status = await timed(cli, read)
            out.record(kind, read=name, cache="cold", run=run, ms=round(took, 2), rows=rows,
                       status=status, ready_after_s=round(ready, 1))
        print(f"  {name}: cold done", file=sys.stderr)


class Sampler:
    """CPU and memory of the named containers, from ``docker stats``, while on."""

    def __init__(self, containers: list[str], out: Out) -> None:
        self.containers, self.out, self.step = containers, out, ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, step: str) -> None:
        if not self.containers:
            return
        self.step = step
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                text = subprocess.run(
                    ["docker", "stats", "--no-stream", "--format",
                     "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}", *self.containers],
                    capture_output=True, text=True, timeout=30, check=True).stdout
            except (subprocess.SubprocessError, OSError) as exc:
                self.out.record("resources_error", step=self.step, error=type(exc).__name__)
                self._stop.wait(5)
                continue
            for line in text.splitlines():
                name, cpu, mem = line.split("|")
                self.out.record("resources", step=self.step, container=name,
                                cpu_cores=round(float(cpu.rstrip("%")) / 100, 3),
                                memory_mib=round(_mib(mem.split("/")[0].strip()), 1))
            self._stop.wait(1)


def _mib(text: str) -> float:
    units = {"B": 1 / 2**20, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "TiB": 2**20,
             "kB": 1e3 / 2**20, "MB": 1e6 / 2**20, "GB": 1e9 / 2**20}
    m = re.fullmatch(r"([\d.]+)\s*([A-Za-z]+)", text)
    return float(m.group(1)) * units.get(m.group(2), 0) if m else 0.0


async def users_step(make: Callable[[], GraphosClient], reads: dict[str, Read], users: int,
                     args: argparse.Namespace, out: Out) -> None:
    names = sorted(reads)
    deadline = time.monotonic() + args.step_seconds

    async def user(u: int) -> None:
        rng = random.Random(args.seed * 1000 + users * 100 + u)
        async with make() as cli:
            while time.monotonic() < deadline:
                name = rng.choice(names)
                took, _rows, status = await timed(cli, reads[name])
                elapsed = args.step_seconds - (deadline - time.monotonic())
                out.record("concurrency", users=users, user=u, read=name, ms=round(took, 2),
                           status=status, t_s=round(elapsed, 2))
                if args.think_ms:
                    await asyncio.sleep(rng.uniform(0.5, 1.5) * args.think_ms / 1000)

    await asyncio.gather(*(user(u) for u in range(1, users + 1)))


async def _job(cli: GraphosClient, submit: Awaitable[Any]) -> tuple[dict[str, Any], float]:
    """Run a job in the background and wait: (its result, seconds from submit to end)."""
    t0 = time.perf_counter()
    job = await cli.wait_for_job((await submit).id, poll_s=0.5)
    if job.status != "succeeded" or job.result is None:
        raise SystemExit(f"{job.kind} {job.status}: {job.error}")
    return job.result, time.perf_counter() - t0


async def jobs(cli: GraphosClient, args: argparse.Namespace, out: Out) -> None:
    """The graph metrics, then the community recompute: a run and a re-run each,
    as background jobs (a large tenant outlasts a request's timeout)."""
    for run in ("run", "re-run"):
        r, took = await _job(cli, cli.submit_compute_metrics(metrics=args.metrics.split(",")))
        out.record("job", job="graph metrics", run=run, s=round(took, 2), **_times(r),
                   nodes=r["n_nodes"], edges=r["n_edges_scanned"], nodes_rewritten=r["n_changed"])
        print(f"  graph metrics {run}: {took:.1f} s", file=sys.stderr)
    if not (await cli.info()).get("features", {}).get("louvain"):
        out.record("job_skipped", job="communities", why="the louvain extra is not installed")
        return
    for run in ("run", "re-run"):
        r, took = await _job(cli, cli.submit_recompute_communities(algorithm=args.algorithm))
        ran = r.get("algorithm", "louvain")
        if args.algorithm and ran != args.algorithm and run == "run":
            out.record("job_note", note=f"{args.algorithm} asked, {ran} ran "
                                        "(a server before 0.14.0 has Louvain only)")
        out.record("job", job=f"communities ({r.get('algorithm', 'louvain')})", run=run,
                   s=round(took, 2), **_times(r), nodes=r["n_nodes"], edges=r["n_edges_scanned"],
                   communities=r["n_communities"], modularity=round(r["modularity"], 4),
                   from_scratch=r.get("from_scratch", True))
        print(f"  communities {run}: {took:.1f} s", file=sys.stderr)


def _times(result: dict[str, Any]) -> dict[str, float]:
    return {"server_s": round(result["elapsed_ms"] / 1000, 2),
            "writeback_s": round(result["writeback_ms"] / 1000, 2)}


# ── Tables and summary ───────────────────────────────────────────────


def rows_of(out: Out, kind: str) -> list[dict[str, Any]]:
    path = out.folder / "raw.jsonl"
    return [r for r in map(json.loads, path.open(encoding="utf-8")) if r["kind"] == kind]


def report(out: Out, plan: Plan, args: argparse.Namespace) -> None:
    env = [[k, v] for k, v in plan.facts.items()] + [
        ["runs per read (warm)", args.runs], ["warm-up runs per read", args.warmup],
        ["cold runs per read", args.cold_runs if args.cold_cmd else 0],
        ["users per step", ", ".join(map(str, args.users)) or "none"],
        ["seconds per step", args.step_seconds],
        ["think time (ms, ±50 %)", args.think_ms], ["measured at", now()]]
    env += [[f"skipped: {k}", v] for k, v in plan.skipped.items()]
    env += [["community algorithm", r["note"]] for r in rows_of(out, "job_note")]
    if args.show_names:
        env += [[f"name: {k}", v] for k, v in plan.names.items()]
    out.table("environment", ["fact", "value"], env)

    def per_read(kind: str) -> list[list[Any]]:
        by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for r in rows_of(out, kind):
            by[(r["read"], r["cache"])].append(r)
        table = []
        for (name, cache), rs in by.items():
            good = [r["ms"] for r in rs if r["status"] == "ok"]
            rows = sorted(r["rows"] for r in rs if r["rows"] is not None)
            label = plan.names.get(name, name) if args.show_names and kind == "saved" else name
            table.append([label, cache, len(rs), *ms(good), rows[len(rows) // 2] if rows else "",
                          sum(r["status"] != "ok" for r in rs)])
        return table

    header = ["read", "cache", "runs", "median_ms", "p95_ms", "min_ms", "max_ms", "rows_median",
              "errors"]
    reads, saved = per_read("read"), per_read("saved")
    out.table("reads", header, reads)
    out.table("saved_queries", header, saved)

    conc = []
    for users in args.users:
        rs = [r for r in rows_of(out, "concurrency") if r["users"] == users]
        if not rs:
            continue
        good = [r["ms"] for r in rs if r["status"] == "ok"]
        conc.append([users, len(rs), sum(r["status"] != "ok" for r in rs),
                     *([round(statistics.median(good), 1), round(pct(good, 95), 1),
                        round(pct(good, 99), 1)] if good else ["", "", ""]),
                     round(len(rs) / args.step_seconds, 1)])
    out.table("concurrency", ["users", "requests", "errors", "p50_ms", "p95_ms", "p99_ms",
                              "throughput_per_s"], conc)

    res = []
    by_res: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows_of(out, "resources"):
        by_res[(r["step"], r["container"])].append(r)
    for (step, container), rs in by_res.items():
        cpu = [r["cpu_cores"] for r in rs]
        shown = container if args.show_names or container not in args.docker else (
            f"container {args.docker.index(container) + 1}")
        res.append([step, shown, len(rs),
                    round(statistics.mean(cpu), 2), round(p95(cpu), 2), round(max(cpu), 2),
                    round(max(r["memory_mib"] for r in rs), 1)])
    out.table("resources", ["step", "container", "samples", "cpu_mean_cores", "cpu_p95_cores",
                            "cpu_max_cores", "memory_max_mib"], res)

    job_rows = [[r["job"], r["run"], r["s"], r["server_s"], r["writeback_s"], r["nodes"],
                 r["edges"], r.get("nodes_rewritten", ""), r.get("communities", ""),
                 r.get("modularity", ""),
                 {True: "yes", False: "no"}.get(r.get("from_scratch"), "")]
                for r in rows_of(out, "job")]
    out.table("jobs", ["job", "run", "seconds", "server_seconds", "writeback_seconds", "nodes",
                       "edges", "nodes_rewritten", "communities", "modularity", "from_scratch"],
              job_rows)

    md = [(f"# Tenant measurements\n\nMeasured {now()} with `scripts/bench_tenant.py`. "
           "Statistics only; the CSVs next to this file hold every table, `raw.jsonl` every "
           "measurement.\n"),
          "## Environment\n", "| Fact | Value |", "|---|---|",
          *[f"| {k} | {v} |" for k, v in env], ""]

    def md_table(title: str, head: list[str], rows: list[list[Any]]) -> None:
        if rows:
            md.extend([f"## {title}\n", "| " + " | ".join(head) + " |", "|" + "---|" * len(head),
                       *["| " + " | ".join(map(str, r)) + " |" for r in rows], ""])

    md_table("Reads, one user", header, reads)
    md_table("Saved queries, one user", header, saved)
    md_table("Concurrent users", ["users", "requests", "errors", "p50 ms", "p95 ms", "p99 ms",
                                  "per second"], conc)
    md_table("Resources during the concurrency steps", ["step", "container", "samples",
                                                        "CPU mean", "CPU p95", "CPU max",
                                                        "memory max MiB"], res)
    md_table("Graph jobs (server s: the job itself; s: submit to done, polled every 0.5 s)",
             ["job", "run", "s", "server s", "write-back s", "nodes", "edges",
                            "nodes rewritten", "communities", "modularity", "from scratch"],
             job_rows)
    (out.folder / "summary.md").write_text("\n".join(md), encoding="utf-8")


# ── Entry point ──────────────────────────────────────────────────────


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("base", help="graphos-api's base URL, e.g. http://localhost:8080")
    p.add_argument("tenant")
    p.add_argument("--out", help="output folder (default bench-<tenant>-<UTC time>)")
    p.add_argument("--runs", type=int, default=30, help="recorded runs per read (30)")
    p.add_argument("--warmup", type=int, default=3, help="unrecorded runs before them (3)")
    p.add_argument("--reads", help="only these reads, comma-separated")
    p.add_argument("--users", type=lambda s: [int(u) for u in s.split(",") if u],
                   default=[1, 5, 10, 20], help="concurrent users per step (1,5,10,20); '' skips")
    p.add_argument("--step-seconds", type=float, default=60, help="length of each step (60)")
    p.add_argument("--think-ms", type=float, default=0, help="pause between a user's reads (0)")
    p.add_argument("--cold-cmd", help="shell command run before each cold read (restart, caches)")
    p.add_argument("--cold-runs", type=int, default=3, help="cold runs per read (3)")
    p.add_argument("--ready-timeout", type=float, default=600,
                   help="seconds to wait for /readyz after --cold-cmd (600)")
    p.add_argument("--docker", action="append", default=[], metavar="CONTAINER",
                   help="sample this container's CPU and memory during the steps (repeatable)")
    p.add_argument("--jobs", action="store_true",
                   help="also time the graph metrics and community recompute (writes; maintain)")
    p.add_argument("--metrics", default="degree,pagerank,components", help="metrics for --jobs")
    p.add_argument("--algorithm", choices=["louvain", "leiden"],
                   help="community algorithm for --jobs (default: the tenant's)")
    p.add_argument("--saved-params", help="JSON file: {saved query name: {param: value}}")
    p.add_argument("--start-label", help="the label to start reads from (default: largest linked)")
    p.add_argument("--start-id", help="the node to start reads from (default: sampled)")
    p.add_argument("--sample", type=int, default=50, help="nodes sampled to pick the start (50)")
    p.add_argument("--seed", type=int, default=1, help="seed of the users' read order (1)")
    p.add_argument("--timeout-s", type=float, default=120, help="per-request timeout (120)")
    p.add_argument("--show-names", action="store_true",
                   help="write labels, ids, words and container names into the output")
    p.add_argument("--list-saved", action="store_true",
                   help="print the saved queries and their parameters, as a --saved-params "
                        "template, and stop")
    args = p.parse_args(argv)
    args.base = args.base.rstrip("/")
    return args


async def main(args: argparse.Namespace) -> Path:
    token = os.environ.get("GRAPHOS_TOKEN")
    auth = None
    if not token and os.environ.get("GRAPHOS_USER"):
        auth = PasswordAuth(os.environ["GRAPHOS_USER"], os.environ["GRAPHOS_PASSWORD"])

    def make() -> GraphosClient:
        return GraphosClient(base_url=args.base, tenant=args.tenant, token=token, auth=auth,
                             timeout_s=args.timeout_s, retries=0)

    if args.list_saved:
        async with make() as cli:
            saved = (await cli.schema_summary()).saved_queries
        print(json.dumps({q.name: {p: f"<{t}>" for p, t in q.params.items()} for q in saved},
                         indent=2))
        raise SystemExit(0)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Out(Path(args.out or f"bench-{args.tenant}-{stamp}"))
    try:
        async with make() as cli:
            print("learning the tenant", file=sys.stderr)
            plan = await discover(cli, args)
            print(f"reads: {', '.join(plan.reads)}", file=sys.stderr)
            await one_user(cli, plan.reads, "read", args.runs, args.warmup, out)
            await one_user(cli, plan.saved, "saved", args.runs, args.warmup, out)
        if args.cold_cmd:
            await cold(make, plan.reads, "read", args, out)
            await cold(make, plan.saved, "saved", args, out)
        sampler = Sampler(args.docker, out)
        for users in args.users:
            step = f"{users} user{'s' if users > 1 else ''}"
            print(f"{step} for {args.step_seconds:.0f} s", file=sys.stderr)
            sampler.start(step)
            try:
                await users_step(make, plan.reads, users, args, out)
            finally:
                sampler.stop()
        if args.jobs:
            async with make() as cli:
                await jobs(cli, args, out)
        report(out, plan, args)
    finally:
        out.close()
    return out.folder


if __name__ == "__main__":
    folder = asyncio.run(main(parse()))
    print(f"wrote {folder}", file=sys.stderr)

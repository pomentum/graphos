# ADR-0011: Communities computed from the previous partition (Leiden)

- **Status:** Accepted (2026-10-21)
- **Scope:**
  - the community computation (`louvain_compute.py`): Louvain from scratch today;
  - the recompute's request (`POST /v1/{tenant}/recompute-communities`) and its docs;
  - the tenant registration (a per-tenant default algorithm);
  - what "the same graph gives the same communities" means (concept.md, `louvain.py`).
- **Related:** ADR-0010 (rejected: the churn, not the storage, is the cost); concept §5
  (communities), §8; `analysis.greene_match` (stable ids)

## Context

A community recompute writes `community_id` on every node whose community changed, and both
community columns on every edge whose ends' communities changed. On an unchanged graph that is
nothing. After a small change it is most of the graph:

| 250,000 nodes / 950,000 edges | Nothing changed | +1 % edges | +10 % edges |
|---|---:|---:|---:|
| nodes rewritten | 0 | 190,911 (76 %) | 190,891 (76 %) |
| run (write-back) | 24 s (11 s) | 103 s (91 s) | 131 s (108 s) |
| communities kept / new | 64 / 0 | 1 / 61 | 1 / 46 |

At 1M nodes and 3.8M edges the same 1 % change took 469 s, 407 s of it writing.

The ids are not lost by the matching: Louvain lands on another partition. Measured offline with
graphos's own computation, after a 1 % change only 37 % of nodes stay with their best-matching
community, about what a change of seed gives on the same graph (35 %). The partition is one of
many of almost equal modularity, and the run starts from scratch each time. A looser matching
threshold does not help: with Jaccard ≥ 0.1 instead of 0.5, 29 % of nodes keep their id
instead of 24 %.

## Decision

Proposed: add Leiden (igraph's `community_leiden`, modularity objective, the same weights and
resolution), started from the stored partition, and keep Louvain: the request's new
`"algorithm"` chooses, `"leiden"` or `"louvain"`.

- `"louvain"`, the default, is today's computation, unchanged: from scratch, the same graph and
  seed giving the same partition. A request that names no algorithm gets exactly what it gets
  today, so nothing changes for an existing caller.
- `"leiden"`, asked for by name, starts from the stored partition as below. A consumer that keys
  on community ids opts in.
- **Per tenant.** A tenant's registration may set its own default,
  `"community_algorithm": "leiden"` (stored with its other settings in
  `graphos_meta.tenant_settings`, changed at `maintain` level like the rest of the spec). The
  algorithm of a run is the request's `"algorithm"` if given, else the tenant's setting, else
  `"louvain"`. A consumer that keys on community ids sets it once per tenant, and every
  recompute, its jobs included, uses it.
- `"leiden"` calls igraph's `community_leiden` with `objective_function="modularity"` (its
  default is CPM, which would not compare with Louvain), the request's weights and resolution,
  and `initial_membership` from the stored partition; Louvain (`community_multilevel`) has no
  such option, which is why it cannot start from one.
- The run that wrote the stored communities records its algorithm and scope with them
  (`analysis_runs`), and the response says which algorithm ran and whether it started from
  scratch.
- The initial membership is each node's stored `community_id`; a node without one (new, or in a
  community below `min_community_size`) starts on its own. Leiden then moves only the nodes the
  graph change gives a reason to move, and the stable-id matching (`greene_match`) keeps the ids
  as today.
- A tenant with no stored communities, or a request with `"from_scratch": true` (new), starts
  from scratch with the seed, as today.
- A change of `resolution`, `rel_types`, `rel_weights` or `node_filter` from the stored run
  starts from scratch: the stored partition answers another question.

## Decision options

1. **Leiden from the previous partition, Louvain kept selectable (proposed).** Stable, faster,
   and at least as good a partition (below); the result depends on the stored partition, not
   only on the graph and the seed. A consumer that needs today's from-scratch result, or wants
   to compare, asks for `"louvain"`.
2. **Leiden from the previous partition, replacing Louvain.** Simpler, but takes away a result
   consumers may rely on, with no way back.
3. **Louvain from scratch, looser id matching.** Measured: 29 % of ids kept at best. The
   partition itself moves; no matching can keep what is not there.
4. **Louvain from scratch with a fixed node order and seed.** That is today: the same graph gives
   the same partition, but a small change gives another one.
5. **Leiden from scratch.** Faster than Louvain (3.8 s against 16.7 s here) but as unstable
   (34 % of nodes stay).

## Evidence

Offline, on the graph above, with graphos's `build_graph` and seed, after the partition has
settled (Leiden from the previous partition, run until it no longer improves):

| After +1 % edges | Modularity | Time | Nodes kept together | Ids kept (θ = 0.5) |
|---|---:|---:|---:|---:|
| Louvain from scratch (today) | 0.420 | 16.7 s | 37 % | 24 % |
| Leiden from scratch | 0.420 | 3.8 s | 34 % | 24 % |
| Leiden from the previous partition | 0.437 | 1.4 s | 97.5 % | 97.5 % |

The first runs from the previous partition still raise modularity (0.433, 0.435, 0.436), then
it settles; a change then moves only the nodes it concerns.

Then measured on the server, built from this decision, on the same graph (one run each step,
Leiden from the stored partition after a switch-over run):

| Recompute | Louvain (0.13.2) | Leiden |
|---|---:|---:|
| nothing changed: nodes rewritten | 0 | 8,005 (3.2 %; the partition still settling) |
| +1 % edges: nodes / edges rewritten | 190,911 / 1,415,795 | 5,083 / 46,406 |
| +1 % edges: run | 103 s | 28 s |
| +10 % edges: nodes / edges rewritten | 190,891 / 1,586,721 | 7,076 / 226,797 |
| +10 % edges: run | 131 s | 43 s |
| ids kept after +1 % | 1 of 62 | 63 of 63 |
| modularity | ≈0.42 | 0.437 → 0.442 over the runs |

## Consequences

- A recompute after a small change rewrites a few percent of the nodes and the edges at their
  ends, instead of three quarters of the graph; the write-back, most of a run, shrinks with it.
- Community ids are stable across small changes, which is what a consumer keying on them (a
  saved query, a colour, a retrieval boost per community) needs.
- The result depends on the history: two tenants with the same graph can hold different, equally
  good partitions. "The same graph and seed give the same communities" holds only for a run from
  scratch; concept.md and `louvain.py` must say so.
- The endpoint and the job keep their names (`recompute-communities`); the docs and the UI name
  both algorithms, Louvain as the default; the client gains the `algorithm` and `from_scratch`
  fields, and the registration its `community_algorithm`. No existing request or registration
  changes meaning: a non-breaking change.
  python-igraph stays the only dependency.
- **Licence and packaging unchanged.** Leiden is python-igraph's `community_leiden`, in the
  same optional `louvain` extra (GPL-2.0-or-later) as Louvain, in the pinned 0.11 series: no new
  dependency. Both are there exactly when the extra is installed, and without it the endpoint
  answers 501 for either, as today. The extra, `docker-compose.louvain.yml` and
  `features.louvain` keep their names, so no install breaks; `GET /v1/info` also lists the
  algorithms available (`features.community_algorithms`).
- Switching algorithm between runs starts from scratch for Leiden (the stored partition came from
  the other one) and changes the ids no more than a Louvain run does today.

## Decided

- **Iterations:** two per Leiden run (`LEIDEN_ITERATIONS`, igraph's default): 96 % of nodes kept
  against 97.5 % with no cap, at the same modularity, and a long series of runs improves the
  partition step by step instead of reshaping it.
- **The default:** `"louvain"`, so the change breaks nothing. Whether to make `"leiden"` the
  default later is a decision for a later release, once callers have used it.
- **Per tenant:** `community_algorithm` in the registration, never empty (`"louvain"` for a new
  tenant and for one registered before this).

## Open questions

- Measured on synthetic data only; a graph with clearer communities moves less from scratch, so
  the gain there may be smaller.
- On an unchanged graph Leiden still moves a few percent of the nodes while its partition
  settles (Louvain none). A run could be skipped when nothing was written since the last one
  with the same parameters; not done here.
- The web UI recomputes with the tenant's algorithm but still names only Louvain in its texts;
  naming both, and offering the choice there, is follow-up work (with the screenshots it changes).

## Acceptance

- On the graph above, a recompute after 1 % more edges rewrites under 10 % of the nodes, and
  its modularity is not below Louvain's from scratch.
- A first run, `from_scratch`, and a run after a change of scope or algorithm start from
  scratch; the tests cover each.
- A tenant registered with `"community_algorithm": "leiden"` runs Leiden when the request names
  none, and Louvain when the request asks for it.
- `"algorithm": "louvain"` gives exactly today's partition for the same graph and seed (a test
  compares it with the current computation).
- concept.md, usage.md, features.md, algorithms.md, the UI's wording and the CHANGELOG say what
  changed.

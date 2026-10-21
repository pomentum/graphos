# ADR-0010: Analysis results outside `properties`

- **Status:** Rejected (2026-10-21): the cost of a re-run is how many rows change, not where
  they are stored; see *Why rejected* below.
- **Scope:**
  - where graph metrics (`graphos_*`) and `community_id` are stored on vertices, and
    `end_community_id` / `start_community_id` on edges;
  - every read that returns or filters on them: node reads, `nodes:scan`, search, the graph
    map, dataset export and import, the Cypher console;
  - the write-backs in `writes.py` (`set_node_metrics`, `set_node_communities`,
    `set_edge_end_communities`).
- **Related:** concept §2.3 (storage), §8 (known limitations); ADR-0001 (one writer of a row);
  [services/graphos/DEVELOPMENT.md §3](../../../services/graphos/DEVELOPMENT.md#3-data-model-invariants)
  (reserved property names, every row write bumps `graphos_rev`)

## Why rejected

The figures below are first runs. Re-runs were measured before deciding: on the same setup, a
graph of 250,000 nodes and 950,000 edges, with 1 % and 10 % new edges added between runs.

| Re-run | Nothing changed | +1 % edges | +10 % edges |
|---|---:|---:|---:|
| metrics: nodes rewritten | 0 | 250,076 (100 %) | 250,079 (100 %) |
| metrics: run (write-back) | 10 s (5 s) | 41 s (35 s) | 46 s (40 s) |
| communities: nodes rewritten | 0 | 190,911 (76 %) | 190,891 (76 %) |
| communities: run (write-back) | 24 s (11 s) | 103 s (91 s) | 131 s (108 s) |

An unchanged graph costs little, but a 1 % change rewrites most of the graph again, because the
values themselves change on most nodes:

- **PageRank** moves by a little on almost every node when edges are added (94 % by more than
  0.1 %), and is stored to six significant digits, so every one of them is written; only 4.7 %
  move by more than 1 %.
- **Communities** are recomputed from scratch, and Louvain lands on another partition: after a
  1 % change only 37 % of nodes stay with their best-matching community, as after a change of
  seed on the same graph (35 %). The ids cannot be carried over, so most nodes get a new one.

A side table would make each of those rewrites cheaper but would still rewrite most of the graph,
and it would take the results out of reach of Cypher. The cost is the churn, so the churn is what
to reduce, with the results left where they are: a relative tolerance below which a metric is
not rewritten (`GRAPHOS_METRICS_MIN_CHANGE`), and communities computed from the previous
partition (ADR-0011).

## Context

A metrics run and a community recompute write their results back onto the graph. On a large
tenant that write-back is almost the whole run:

| Synthetic graph, first run | Run | Write-back | Share |
|---|---:|---:|---:|
| about 1M nodes / 3.8M edges, metrics (degree, PageRank, components) | 131 s | 114 s | 87 % |
| the same, community recompute | 468 s | 418 s | 89 % |

The rows to write are found quickly: the staged `(graphid, values)` pairs join to the label
tables through each table's primary key on `id`. A batch of 25,000 vertices takes 2–4.5 s, of
which the lookup is under 0.3 s. The rest is the update itself.

The values live inside `properties` (vertices) or in indexed columns (edges). Changing either
makes every update a non-HOT one: PostgreSQL writes a new entry into **every** index of the
table for the new row version, whether or not that index's value changed. A vertex label
carries its primary key, the `id` unique index, the `community_id` expression index, a GIN
index over all of `properties`, and each property, search and trigram index the tenant
registered: 7–9 indexes, two or more of them GIN, in the graph measured. That is about 34
buffer touches per row written. The GIN index over `properties` also indexes every
`graphos_*` key itself.

`VACUUM (ANALYZE)` after a large write-back (next release) stops the tables growing run after run,
but each run still pays the index writes.

## Decision

Proposed: keep analysis results in a side table per tenant, not in the rows of the graph.

- `"<tenant>"._graphos_vertex_results (id graphid PRIMARY KEY, community_id int,
  metrics jsonb)`, with an index on `community_id`; for edges,
  `"<tenant>"._graphos_edge_results (id graphid PRIMARY KEY, start_community_id text,
  end_community_id text)` with the two partial indexes the edge tables carry today.
- The write-backs write only these tables: one primary key, one or two small indexes. The
  graph's rows, their revisions and their indexes are not touched; the change record stays one
  summary per run, as today.
- Reads that return a node merge the side table's values into `properties` as today, so the
  wire contract does not change: `graphos_pagerank` and `community_id` still appear on a node
  and in `nodes:scan` results, `order_by` and `where` on them still work (through a join).
- Deleting a node or edge deletes its results (the write path in `writes.py` does both in one
  transaction).

## Decision options

1. **Side table (proposed).** The update touches only the side table's indexes; the graph's
   rows are not rewritten at all. Every read that exposes the values needs a join.
2. **Dedicated, unindexed columns on the label tables** (`graphos_metrics jsonb`). An update of
   an unindexed column can be HOT (no index writes) when the page has room, but `community_id`
   must stay indexed for the community reads, and any indexed column changing puts every index
   back in the update. Also widens every row of the graph.
3. **Keep `properties`, drop the GIN index over all of `properties`.** Cheaper updates, but
   the GIN index serves containment filters on any property; dropping it is a loss for every
   tenant, and the other indexes still take an entry per row.
4. **Status quo with `VACUUM` after a write-back.** Done (next release); bounds the growth, not the
   time.

## Evidence

- The run timings above, on a synthetic graph at 1M nodes, with the release's PostgreSQL
  settings (`shared_buffers` 1 GB) and no other load.
- One write-back batch measured with `EXPLAIN (ANALYZE, BUFFERS)` in a rolled-back transaction:
  25,000 vertices, 2–4.5 s; the join through the primary keys about 450,000 buffer hits, the
  update about 850,000.

## Consequences

- Metrics and community write-backs cost in proportion to the rows changed times one or two
  small indexes, instead of times every index of the graph.
- A write-back no longer bumps `graphos_rev` on the nodes it changes: results are not the
  consumer's data, and a consumer's If-Match no longer fails because a metrics run touched the
  node. This changes the invariant "every row write bumps `graphos_rev`" for analysis results
  only, and must be said in concept.md.
- The Cypher console reads AGE's tables: `n.graphos_pagerank` and `n.community_id` would no
  longer be visible to Cypher. Either the console documents that (and points to `nodes:scan`),
  or saved queries over results are given another way. This is the main cost.
- Dataset export and import carry the results from the side table (`computed=true`), as today.
- An upgrade moves the values out of `properties` into the side table once, per tenant, at
  start-up; a rollback needs the reverse.

## Open questions

- Is losing the results from Cypher acceptable, or should the console join the side table
  (which would mean a graphos-specific function in Cypher, against ADR-0006's guards)?
- One side table per tenant, or per label (simpler joins, more tables)?
- Should the edge community columns move too, or stay as columns with their indexes made
  partial on a smaller predicate?

## Acceptance

- The 1M-node first metrics run's write-back at least 5 times faster on the same setup, and a
  recompute on an unchanged graph still writes nothing.
- Every read that returns `graphos_*` or `community_id` today returns the same values;
  `tests/test_graph_metrics.py` and the community tests pass unchanged at the API level.
- concept.md §2.3 and §8, usage.md, features.md and the upgrade note describe the change.

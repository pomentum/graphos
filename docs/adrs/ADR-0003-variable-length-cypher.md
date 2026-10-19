# ADR-0003: Variable-length Cypher patterns in the console

- **Status:** Proposed (2026-10-16)
- **Scope:** the Cypher console's guard (`console.needs_age_graph`), its pool and settings; an
  upstream change to Apache AGE; the AGE pin
- **Related:** concept §8 limitation 11; apache/age#2583; ADR-0002 (`run_cypher` over MCP)

## Context

The console refuses variable-length patterns (`-[*1..3]->`) and `vertex_stats()`
(`console.needs_age_graph`). AGE serves both from an in-memory copy of the whole graph, and the
loader cannot load graphos's tables.

**What the loader requires:** AGE's global-graph loader (`age_global_graph.c`,
`load_vertex_hashtable` / `load_edge_hashtable`) refuses a label table unless it has exactly AGE's
columns:
- `natts == 2` for vertices;
- `natts == 4` for edges.

**What graphos adds:** columns of its own (`graphos_rev`, the community columns, generated
columns), all **after** AGE's.

**The fixed positions:** the loader reads `id`, `start_id` and `end_id` at positions 0–2 (checking
their names), and the properties at the last of AGE's positions. Those positions do not move.

**Three more facts shape the decision.**

**1. On AGE 1.6, a failed load hangs the backend.**
- The load ran under a process-wide mutex that the error path never released.
- The next variable-length query in that backend blocked forever, beyond cancel and terminate.
- AGE 1.8 removed that mutex (apache/age#2433), so on 1.8 a failed load is only an error.
- graphos's guard exists partly because of this, and 1.6 is still supported (deprecated).

**2. The load is not interruptible.** The loader loops have no `CHECK_FOR_INTERRUPTS`. A
`statement_timeout` or a cancel takes effect only after the whole graph is loaded.

**3. The copy is per connection and is kept fresh by triggers.**
- **Lifetime:** every backend that runs such a query holds its own copy, one per graph it touched,
  until the backend ends or `age_delete_global_graphs` frees it.
- **Freshness:** AGE 1.8 keeps the copy fresh through a statement-level trigger,
  `_age_cache_invalidate`, on every label table. SQL writes, such as graphos's, then invalidate it.
- **The gap:**
  - tenants created on 1.8 have the trigger;
  - tenants moved from PostgreSQL 16 by graphos's upgrade do not (0 of 22 tables in one moved
    tenant), because neither the dump nor the restore installs it.
  - graphos 0.11.1 installs it, idempotently, after the move and at start.

## Decision

Lift the refusal **where it is safe**, through the upstream fix, not a graphos-owned AGE build or
a pattern compiler.

### 1. Upstream: relax the check, and make the load interruptible

Propose to AGE (on #2583, then as a pull request):
- **The column check:** check that the **leading** columns are AGE's by name (`id`, `properties` for
  vertices; `id`, `start_id`, `end_id`, `properties` for edges), and ignore columns after them,
  instead of requiring an exact count. A table that is not AGE's shape is still refused.
- **Interrupts:** add `CHECK_FOR_INTERRUPTS()` in the vertex and edge load loops, so a timeout or a
  cancel stops a long load.
- **Tests:**
  - a graph with an extra column on the parent tables;
  - one with an extra column on one label table;
  - a generated column;
  - a cancel during the load.

### 2. graphos: the conditions for running variable-length patterns

The console runs a variable-length pattern, or `vertex_stats()`, only when **all** of these hold.
Otherwise it refuses as today, and the refusal names the condition that failed and points to the
API's `expand` and `paths`.

| Condition | Rule | Setting (default) |
|---|---|---|
| The switch | `auto`: on when the AGE version qualifies; `off`: never | `GRAPHOS_CYPHER_VLE` (`auto`) |
| The AGE version | in graphos's list of AGE releases with the fix. **Detected from the extension version, never by running a probe query**: on 1.6, a probe's failure would hang the backend | built in; AGE 1.6 is never on it |
| The triggers | every label table of the tenant has `_age_cache_invalidate`. Checked once per tenant and cached; refreshed on a schema change | none |
| The tenant's size | edges ≤ the cap. About 125 bytes per edge measured, so the default is about 250 MB per connection | `GRAPHOS_CYPHER_VLE_MAX_EDGES` (2,000,000) |
| The pattern | every variable-length relationship has an upper bound (`*..n` or `*m..n`), with n ≤ the cap. An unbounded `*` is refused | `GRAPHOS_CYPHER_VLE_MAX_HOPS` (6) |

**The existing guard still applies in full:**
- read-only, refused before the database;
- Cypher's own functions only;
- `BEGIN READ ONLY`, the row cap, `statement_timeout` and the watchdog.

### 3. graphos: bounded memory

**A pool of its own:** variable-length queries run on a separate console pool,
`GRAPHOS_CYPHER_VLE_POOL_SIZE` (default 2). They do not hold the general console connections
(`GRAPHOS_CYPHER_POOL_SIZE`, default 5).

**One graph per connection:**
- a connection holds at most one tenant's graph;
- before running on tenant T, a connection whose last variable-length tenant was another one calls
  `ag_catalog.age_delete_global_graphs('<that graph>')`.

**The bound:** the memory is then at most the pool size × the size cap (about 500 MB by default),
whatever the number of tenants.

**Idle connections:** idle VLE connections are closed after `GRAPHOS_CYPHER_VLE_IDLE_S`
(default 300), which frees their graphs.

### 4. The timeout budget

**Where the time goes:** the first call on a connection includes the load (2.1 s at 1M edges,
9.9 s at 3M). At the default cap, it fits well inside the console's `statement_timeout` (30 s).

**Until the load is interruptible:**
- a timeout that fires during a load takes effect after the load;
- the size cap bounds that delay;
- the watchdog's terminate stays the backstop.

### Not chosen now

**A graphos-built AGE image with the fix:**
- graphos would then own an AGE build: security updates, PostgreSQL minors, CI for both versions;
- only if the upstream fix has not landed one AGE release after it is proposed.

**A graphos compiler for a bounded pattern subset onto `expand` and `paths`:**
- no per-connection copy and no size cap;
- but a Cypher parser and a translator to maintain;
- kept for tenants above the cap, or for GQL quantified paths, behind its own ADR.

### Amendment (2026-10-16): the compiler route, measured, as the fallback

An embedded graph library runs read-only Cypher this way: each bounded variable-length relationship becomes one
recursive CTE `(seed, current, edges walked, depth)`, seeded from the more selective end, an edge
never reused within a trail, and an unbounded `*` answered only after a depth+1 probe proves no
trail is longer than the cap. Measured on graphos's table shape (see Evidence):
- **correct:** trail and end-node counts equal to AGE's own engine in 16 of 16 cases;
- **cheap at query time**, with no copy of the graph per connection and no tenant size cap, and it
  runs on AGE 1.6;
- **but trails explode** on undirected patterns from a hub, so it needs a cap on intermediate rows.

**What this changes:** route 1 (the upstream fix) stays the default. The compiler, until now "not
chosen", becomes the planned fallback where route 1 cannot serve:
- tenants above `GRAPHOS_CYPHER_VLE_MAX_EDGES`;
- AGE 1.6, where the upstream fix will never land;
- the period before an AGE release carries the fix.

**Its scope**, kept small so it is not a Cypher engine:
- one `MATCH` of the shape `(a)-[:T1|T2*m..n]->(b)` (any direction), `a` pinned by `id` or by an
  indexed property, optional label on `b`, `WHERE` on `b`'s properties, `RETURN` of `a`, `b`,
  `count`, `DISTINCT` and the path's length;
- the console recognises this shape before it would refuse, and compiles it onto the CTE; every
  other variable-length query is refused as today;
- `GRAPHOS_CYPHER_VLE_MAX_HOPS` (6) applies, plus `GRAPHOS_CYPHER_VLE_MAX_TRAILS` (default 200,000
  intermediate rows; past it the query is refused, never cut short, and the refusal points to
  `expand`, which counts distinct nodes and does not explode);
- the same read-only transaction, row cap, timeout and watchdog.

It gets its own ADR when built; this amendment records why it is now the fallback.

## Decision options

| Option | For | Against |
|---|---|---|
| **Upstream fix, then a size-capped lift (chosen)** | two lines in AGE; graphos keeps using AGE's own variable-length engine; testable now with a patched build | waits on an AGE release; per-connection memory; a cap for large tenants |
| A graphos-built, patched AGE image | available at once | graphos owns an AGE build and its security updates |
| A graphos pattern compiler onto `expand` / `paths` | no per-connection copy, no cap; reusable for GQL | M–L effort; a Cypher parser; semantics to keep equal to AGE's |
| Keep the refusal (status quo) | no work, no risk | a known limitation; plain AGE copies of the data run what graphos refuses |
| Restructure graphos's tables to AGE's exact columns | stock AGE works | `graphos_rev`, the community columns and generated columns are the data model; a large migration for every tenant |

## Evidence

AGE 1.8.0 built from source (`PG18/v1.8.0-rc0`, `e43dc1a`) in a throwaway container, on label
tables carrying graphos-style extra columns.

| Run | Result |
|---|---|
| Stock 1.8.0 | `Invalid number of attributes for gvle.N`: limitation 11 reproduced |
| `!= 2` → `< 2` and `!= 4` → `< 4`, rebuilt, the server restarted (AGE is preloaded) | `MATCH (a)-[:R*1..3]->(b)` returns **14 nodes, the same as a recursive SQL reference**; paths of length 2 are correct |
| A SQL write (a new node and edge), then the pattern again | **15**: the trigger invalidated the copy |
| 1M edges: the first call / later calls | **2.1 s** (the load) / 1.4 ms |
| 3M edges: the first call / later calls | **9.9 s** / 0.9 ms |
| The connection's memory after the first call, 3M edges | **380 MB** (the edge table 192 MB, vertex-to-edge lists 80 MB): about 125 bytes per edge |

**The compiler route** (2026-10-16), stock AGE 1.8.0 image, 270,000 nodes and 1,010,970 edges
(3.7 per node, 200 hub nodes), loaded twice: with `graphos_rev` and graphos's indexes, and as plain
AGE. AGE refuses the first (`Invalid number of attributes`), so its numbers come from the plain copy
and include its seed lookup over all vertices without an index; the CTE is seeded by graphid.

| Pattern, seed | Trails / end nodes (equal in every case) | Recursive CTE | AGE, plain copy |
|---|---|---|---|
| `->`, 2 hops | 15 / 15 | 13 ms | 7.9 s (first call: the load) |
| `->`, 6 hops, ordinary node | 3,020 / 2,384 | 117 ms | 0.57 s |
| `->`, 6 hops, hub | 3,337 / 2,470 | 0.74 s | 1.49 s |
| `-`, 4 hops, hub | 51,047 / 42,186 | 0.28 s | 1.16 s |
| `-`, 5 hops, hub | 404,215 / 175,892 | 2.5 s | 14.2 s |
| `-`, 6 hops, hub | 3,151,701 / 268,656 | **40.2 s, 727 MB of temp files** | not run |

**What the source shows (AGE 1.8):**
- `age_delete_global_graphs(name)` exists and frees one graph's copy;
- the load loops have no interrupt checks.

## Consequences

- **Limitation 11:**
  - it becomes "variable-length patterns run on tenants up to `GRAPHOS_CYPHER_VLE_MAX_EDGES` edges,
    on AGE releases with the fix";
  - above that, the console points to `expand` and `paths`;
  - on AGE 1.6 the refusal stays.
- **ADR-0002's `run_cypher`** follows the same rules, with no MCP-specific path.
- **The AGE pin:**
  - graphos moves its pin (by digest) to the first AGE release with the fix;
  - until then `auto` keeps the refusal, so a graphos release can ship this code before AGE does.
- **The upgrade:** moved tenants get the triggers (graphos 0.11.1). Their absence is reported
  at start, and makes the tenant fail the "triggers" condition rather than serve stale results.
- **Operators:** the memory added is pool size × cap, documented with the settings. Large tenants
  may lower the cap or turn the feature off.

## Acceptance

Tests on PostgreSQL 18 with an AGE build that has the fix (CI builds it until a release ships).
They also run on PostgreSQL 16 / AGE 1.6, where every case must be refused and no query may reach
AGE.

**The results:**
- `*1..3` and fixed-length `*2` patterns return the same nodes as `expand`, and paths as `paths`,
  on a graph with community columns and a generated column.
- **Freshness:** after a write through the API, the next pattern sees it, on a created tenant and on
  a moved one.

**Each refusal case refuses before the database, with the condition named:**
- the size cap;
- an unbounded `*`;
- hops over the cap;
- a missing trigger;
- `GRAPHOS_CYPHER_VLE=off`;
- an AGE version not on the list.

**Memory:**
- two tenants alternating on one connection hold one copy, not two (backend memory measured);
- idle VLE connections close.

**Timeout:** a pattern on a tenant at the cap finishes, or is stopped, inside `statement_timeout`.

## Open questions

1. The default size cap: 2M edges (about 250 MB per connection), or lower for small hosts?
2. A per-tenant override of the cap in the tenant spec, or the global setting only?
3. The compiler fallback (amendment above): build it as soon as route 1 is proposed upstream, or
   only when a tenant above the cap, or on AGE 1.6, asks for variable-length patterns?

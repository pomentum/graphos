# Apache AGE — practices and lessons learned

Hands-on notes from running Apache AGE under graphos's write and read paths:
1.6.0 on PostgreSQL 16 (`apache/age:release_PG16_1.6.0`, the bundled database up to graphos 0.9) and
1.8.0 on PostgreSQL 18 (`apache/age:release_PG18_1.8.0`, bundled since). A pitfall marked *1.6* is
fixed in 1.8 (§11); the rest hold on both. graphos now starts on 1.8 only
([ADR-0007](adrs/ADR-0007-drop-postgresql-16.md)); the 1.6 notes are kept as history, and for
the data a move from 1.6 brings along.
For the write-path templates in more detail see
[`graph_age_practices.md`](./graph_age_practices.md).

---

## 1. Cypher syntax — what AGE rejects

Checked row by row on 2026-10-15, on AGE 1.6.0 (PostgreSQL 16) and AGE 1.8.0 (PostgreSQL 18), on
scratch graphs shaped like graphos's (`graphos_rev` on both parent tables, the two community
columns on edges).

| Pattern | AGE 1.6.0 | AGE 1.8.0 | Workaround |
|---|---|---|---|
| `SET n += $param` (parametric map) | ❌ "SET clause expects a map" | ❌ same | Inline a map literal: `SET n += {k: 'v', …}` (works on both) |
| `SET n += row.props` (`UNWIND` projection) | ❌ "SET clause expects a map" | ❌ same | Same: inline literal, one `cypher()` call per row |
| `ON CREATE SET … ON MATCH SET …` | ❌ syntax error at `ON` | ✅ works | On 1.6: plain `SET` after `MERGE` |
| Multi-label `(:L1:L2 …)` in `CREATE/MERGE/MATCH` | ❌ syntax error | ❌ syntax error | Postgres `INHERIT` between label tables (§3) |
| Several `;`-separated statements in one `cypher(...)` call | ❌ syntax error | ❌ syntax error | One statement per `cypher()` call |
| `WITH …`-chained `MERGE`s for batched writes | ⚠️ super-linear (measured earlier) | not measured | Never chain >1 `MERGE`; separate calls, or COPY (§5) |
| Cypher `SET` / `MERGE … SET` on an **edge** table with extra columns | 💥 backend segfault: the server restarts, every connection drops (reproduced) | ❌ "null value in column graphos_rev" (the row is rebuilt without the extra columns) | Write with plain SQL only (§5) |
| Cypher `SET` / `CREATE` / `MERGE … SET` on a **vertex** table with an extra column | ⚠️ carried out, but the extra columns are not kept up (`graphos_rev` not bumped) | ❌ `SET`, `MERGE … SET`: "null value in column graphos_rev"; `CREATE`: "invalid perminfoindex" | Same |

The right mental model: AGE is a **Postgres extension that transpiles
openCypher to SQL**. Anything that doesn't have a clean SQL lowering
(parameterised maps, chained writes, conditional set clauses) is
either rejected at parse time or compiled to something slow.

graphos writes no rows through Cypher. Every write, on every endpoint, is
SQL in one module, `graphos.writes`: node and edge upserts go through the
COPY-staged path of §6 (`writes.upsert_nodes` / `writes.upsert_edges`), and
node deletes are one SQL statement that removes the nodes and their edges
together. §5 gives the two reasons (throughput and a backend segfault).
Every API read is SQL too; Cypher runs only in the read-only console
(on unless `GRAPHOS_CYPHER=false`). The rows above about inlined map literals and
per-row `cypher()` calls describe what a Cypher write needs on AGE 1.6;
graphos does not write that way. §11 lists which of these AGE has fixed
since, and in which release.

---

## 2. Storage shape (it's just Postgres tables)

For graph `G` and label `L`:

- **Vertex label** → table `"G"."L"` with `(id graphid PK, properties agtype)`
- **Edge label** → table `"G"."L"` with `(id graphid PK, start_id graphid, end_id graphid, properties agtype)`

Every vertex label table inherits from `"G"._ag_label_vertex` and every
edge label table from `"G"._ag_label_edge`, so a scan of the parent sees
all labels.

`graphid` is `_label_id << 48 | nextval(<L>_id_seq)`. Defaults on the
`id` column auto-generate via `_graphid(_label_id(...), nextval(...))`,
so direct `INSERT` *does not need* manual id minting.

Implication: **direct `INSERT` into the label table is fully visible
to AGE's `MATCH`** — verified end-to-end. This is the foundation of
the bulk-load fast path.

---

## 3. Multi-label simulation via `INHERIT`

AGE rejects `MERGE (n:Employee:Person …)` so multi-label patterns can't
be used directly. Equivalent semantics come from Postgres table
inheritance:

```sql
ALTER TABLE "G"."Employee" INHERIT "G"."Person";
```

After that, `MATCH (n:Person)` sees `Employee` rows transparently
(Postgres rewrites the scan to include all inheriting children).
graphos wires this from the tenant's `label_hierarchy` at registration.

**Catch**: writes target the **child** only.
`MERGE (n:Person {id: X})` against a row whose actual label is `Employee`
will **insert a new row into the `Person` table** instead of finding the
inherited child. The per-label unique index (§4) doesn't prevent this
either — Postgres unique indexes are not inherited. So always write to
the most-specific label.

Catalog-safe idempotency: query `pg_inherits` before
`ALTER TABLE ... INHERIT` — there is no `IF NOT EXISTS` form for
inheritance (`graphos.bootstrap._inherit_if_missing`).

---

## 4. Index strategy

Each label table is just a Postgres table — give it the indexes
Postgres would expect. `graphos.bootstrap.bootstrap_tenant` creates these
for every registered label:

| Index | Purpose |
|---|---|
| `GIN (properties)` per vertex label | Containment `@>`: Cypher `MATCH (n:L {k: v})` and graphos's delete-by-property |
| `UNIQUE ((properties->'"id"'))` per vertex label | Arbiter for the `ON CONFLICT` of every node write; serves direct-SQL id lookups and the edge-endpoint JOIN |
| btree on `community_id` (text extract) per vertex label | Community endpoints, community-scoped expansion |
| trigram GIN on `name` per vertex label | `/graph/search` (case-insensitive `ILIKE '%q%'`; `%` and `_` in `q` are escaped, so `q` matches literally) |
| `UNIQUE (start_id, end_id)` per edge label | Arbiter for the `ON CONFLICT` of every edge write |
| `btree (start_id, end_id)` per edge label | Outgoing-edge lookups on read paths (kept alongside the unique one) |
| `btree (end_id)` per edge label | Incoming-edge lookups (`WHERE end_id IN (…)`) |
| partial btree on `end_community_id` and partial btree on `start_community_id` per edge label | Community-scoped BFS (forward and backward hops) |
| `btree (id)` per edge label (`CONCURRENTLY`) | Paging edges in graphid order; AGE does not index the graphid |
| `btree (id)` per vertex label (opt-in, `GRAPHOS_VERTEX_GRAPHID_INDEXES`) | Resolving edge endpoints by graphid; costs 20–30% on bulk vertex writes |

Declared `property_indexes` and `search_indexes` are built at registration as well, and label
tables that were never analyzed are analyzed once. The full list, with names, is in
[concept.md §2.3](./concept.md#23-indexes-created-at-registration).

Two different access paths, two different indexes:

- **Cypher `MATCH (n:L {id: X})` compiles to
  `WHERE properties @> '{"id":"X"}'::agtype`** — the GIN containment
  operator. It does *not* lower to `properties->'"id"' = …`, so the btree
  unique index is **not** used by Cypher MATCH. GIN on `properties` is
  what keeps a Cypher read by id off a sequential scan. (graphos's own reads
  run no Cypher and read by id in SQL, through the unique btree; the
  console's `MATCH {id: …}` uses this GIN index.)
- **Direct SQL** that compares `properties->'"id"'` (node-by-id reads,
  property stamping, the `ON CONFLICT` of every node write, and the edge
  write path's endpoint JOIN) uses the unique btree.

Note the inner double quotes in `properties->'"id"'`: the key is written
as an agtype string literal, not as a bare `'id'` the way you would with
jsonb. Every query that should use the unique index must spell the
expression exactly as the index does — `properties->'"id"'` — or the
planner won't match it.

Plan shape for `MATCH (n:Person {id: …})`:

```text
->  Bitmap Heap Scan on "Person" n
      Recheck Cond: (properties @> '{"id": "..."}'::agtype)
      ->  Bitmap Index Scan on "Person_properties_gin"
            Index Cond: (properties @> '{"id": "..."}'::agtype)
```

**Do not use `>=` against a btree index on an agtype expression.** AGE
1.6.0 and 1.8.0 register `>=` and `>` under each other's btree strategy numbers in
`agtype_ops_btree` (4 is `>`, 5 is `>=`; PostgreSQL's are the other way
round), so an index scan for `expr >= v` starts after the entries equal to
`v` and silently loses them; a sequential scan of the same query returns
them. `>`, `<`, `<=` and `=` are right. graphos's keyset pagination
(`nodes:scan` cursors) therefore never sends `>=`: it queries one branch per
sort key (equalities on the tied keys, then a strict `>`, or `<` when
descending), in order. Fixed on AGE master (apache/age#2577), not yet in a
release (§11). Check it with:

```sql
SELECT a.amopstrategy, a.amopopr::regoperator
FROM pg_amop a JOIN pg_opfamily f ON f.oid = a.amopfamily
WHERE f.opfname = 'agtype_ops_btree' ORDER BY 1;   -- 4 | >   5 | >=
```

**Do not answer "which rows have key K" through this GIN index.** The
key-exists operator `properties ? '"K"'::agtype` can use `gin_agtype_ops`
(its `text` overload cannot), but on AGE 1.6 the index scan misses rows:
on a real tenant it returned 0 rows of a label where a sequential scan
with the same predicate returned 30, for keys holding arrays among
others. graphos does not use it. The resolver's catalogue of which labels
carry which properties is recorded at write time instead
(`graphos_meta.node_properties`).

---

## 5. Why every write is SQL

Every write is SQL in `graphos.writes`, with no flag and no fallback to
Cypher. Every node and edge upsert goes through the COPY-staged
`INSERT … ON CONFLICT` of §6; a merge or patch of one node or edge is a
single-row `UPDATE`, a delete one `DELETE` statement, and `edges:resolve` an
`INSERT … SELECT … ON CONFLICT DO NOTHING`. Two findings led there.

**Reason 1 — throughput.** On a 13k-node + 44k-edge synthetic dataset
(graphos-db with 4 GB shared_buffers, single host). The COPY figures come
from `scripts/bench_bulk_write.py`; the per-row Cypher baseline was measured
with an earlier graphos that still had a Cypher write path, and the script
can no longer reproduce it:

| Approach | Mechanism | Edge throughput |
|---|---|---|
| Per-row `cypher() MERGE` (not used by graphos) | One parse+plan per row, props inlined as a literal | **31 edges/s** |
| **COPY-staged `INSERT … ON CONFLICT`** (current) | One binary COPY into a `TEMP TABLE … ON COMMIT DROP`, then one `INSERT … SELECT … ON CONFLICT` per label / edge type | **1,153–4,840 edges/s** |

That is ≈37–156× in favour of the COPY path on this benchmark.

**Reason 2 — stability.** A Cypher write into a label table with extra
columns crashes the server; see the pitfall below.

Every upsert endpoint uses that path: `POST /v1/{tenant}/graph/nodes`,
`/edges`, `nodes:bulk`, `edges:bulk` and `snapshot-import`. Node upserts
need the unique id index and edge upserts the `UNIQUE (start_id, end_id)`
index from §4; bootstrap installs both, and graphos re-bootstraps every
stored tenant at startup, so a tenant created before those indexes existed
gets them on the next restart.

The `:bulk` endpoints flush every 500 rows per label (nodes) or per
(src label, dst label, edge type) (edges); each flush is its own
transaction. `snapshot-import` buffers up to 5,000 rows per group, flushes
buffered nodes before every edge flush, and commits each batch on its own;
it has the same write path and throughput as the `:bulk` endpoints.

### Pitfall: extra columns on a label table + Cypher writes → a segfault (1.6) or a refusal (1.8)

AGE builds the tuple for a Cypher write from its own idea of the label
table's columns. On AGE 1.6.0, a Cypher `SET` or `MERGE … SET` on an
**edge** table that carries extra columns segfaults the Postgres backend;
the postmaster then restarts the whole server and every connection drops
(reproduced 2026-10-15). On a **vertex** table with one extra column the
same writes, and `CREATE`, are carried out but leave the extra columns as
they were, so graphos's revision is not bumped and the change feed misses
the write.

graphos label tables always carry such columns: bootstrap adds
`graphos_rev` to every label table (through AGE's two parent tables), and
`end_community_id` and `start_community_id` to every edge table (§9,
community-scoped traversal); declared `generated_columns` add more to vertex
labels. AGE 1.8.0 no longer crashes
(apache/age#2458) but rebuilds the row from `id` and `properties` only: a
`SET` writes NULL into the extra columns, which fails on `graphos_rev` (NOT
NULL), and a `CREATE` on a graphos label table fails with `invalid
perminfoindex` (both verified on 1.8.0, PostgreSQL 18). graphos therefore
issues **no Cypher writes**: every node and edge write is the SQL `INSERT … ON CONFLICT
… DO UPDATE SET properties = properties || EXCLUDED.properties` of §6,
which never enters AGE's tuple-building code and keeps the same contract
(additive property merge, missing endpoints counted as skipped). As a
result generated columns on vertex labels are safe: SQL writes fill
them correctly (verified).

The SQL path also sidesteps an AGE 1.6 quirk: `SET r += {…}` on an
already-merged edge intermittently raised `Entity failed to be updated: N`
even though the MERGE succeeded.

### Pitfall: extra columns + variable-length patterns → a backend that never returns

Variable-length patterns (`MATCH p = (a)-[:R*1..3]->(b)`) and
`vertex_stats()` do not read the label tables directly: AGE first loads an
in-memory copy of the whole graph (`manage_GRAPH_global_contexts` →
`load_GRAPH_global_hashtables`, `age_global_graph.c`), holding a
process-wide pthread mutex while it does. The loader refuses a vertex table
that does not have exactly 2 columns, or an edge table that does not have
exactly 4 (`natts` counts dropped columns too). graphos's label tables
carry more (`graphos_rev` on every table, the community columns on edges),
so on a graphos tenant the load always fails: `Invalid number of attributes
for <tenant>._ag_label_vertex`.

The error unwinds past the unlock, so the mutex stays locked in that
backend. The next such query **in the same backend** waits on it forever:
no wait event, no CPU, deaf to `pg_cancel_backend`, `pg_terminate_backend`,
`statement_timeout` and a fast shutdown; only `SIGKILL` (a restart with
crash recovery) ends it. A different backend just gets the error again.
Verified on AGE 1.6.0 with a native stack in `pthread_mutex_lock`
(`age_global_graph.c:847`). In 1.6.0 neither `age_global_graph.c` nor
`age_vle.c` calls `CHECK_FOR_INTERRUPTS()`, so even a successful load of a
large graph cannot be cancelled.

Upstream, AGE 1.8.0 removed the mutex (apache/age#2433) and checks for
interrupts in the variable-length search (apache/age#2430), though not in the
graph load. The column-count check is still there (apache/age#2583, open;
`tupdesc->natts != 2` for vertices and `!= 4` for edges in
`age_global_graph.c`), so variable-length patterns still fail on graphos's
tables, but harmlessly: measured on AGE 1.8.0 / PostgreSQL 18, the query fails
in about 50 ms, the same query again in that session fails at once ("age_vle:
empty graph"), and the session stays usable. On 1.6.0 the second one hung
until the process was killed. This is a limit of the console, not a defect in
graphos: no API read needs AGE's in-memory graph (§11).

graphos runs neither: its paths, expand and other reads are plain SQL. The
Cypher console refuses variable-length patterns and `vertex_stats()` before
they reach the database, and closes, rather than reuses, a connection on
which a query failed. Write the hops out instead
(`-[:R]->()-[:R]->()`, `OPTIONAL MATCH` for the optional ones), or use
`POST /v1/{tenant}/graph/paths`.

### Pitfall: a read-only transaction does not stop AGE 1.6's writes

AGE 1.6 carries out Cypher `SET`, `REMOVE` and `DELETE` (`DETACH DELETE`
too) itself, where PostgreSQL's read-only check does not see them: inside
`BEGIN READ ONLY` they change and delete rows. Only `CREATE` and `MERGE` are
stopped (`cannot execute SELECT in a read-only transaction`). Verified on
AGE 1.6.0 on a scratch graph: `SET` and `REMOVE` changed a node, `DELETE`
and `DETACH DELETE` removed nodes and an edge. AGE 1.8.0 stops all of them.

So a read-only transaction, or a read-only role, is not a guard against
Cypher writes on AGE 1.6. graphos's console refuses the write clauses
before the query reaches the database; a direct-SQL user on AGE 1.6 was not
protected, one reason graphos no longer runs on it.

---

## 6. COPY-staged bulk upsert — the canonical pattern

Vertices (`graphos.writes.upsert_nodes`, used for every node
write on every endpoint):

```sql
BEGIN;
SET LOCAL synchronous_commit = off;      -- safe for idempotent bulk writes

CREATE TEMP TABLE _stg_n (ext_id text, props jsonb) ON COMMIT DROP;

-- asyncpg: await conn.copy_records_to_table('_stg_n', records=…)
-- binary COPY protocol, no agtype round-trip on the hot path

INSERT INTO "G"."L" (properties)
  SELECT (jsonb_set(props, '{id}', to_jsonb(ext_id)))::text::agtype
    FROM (SELECT DISTINCT ON (ext_id) ext_id, props
            FROM _stg_n
           ORDER BY ext_id, ctid DESC) deduped
  ON CONFLICT ((properties->'"id"'))
  DO UPDATE SET properties = "L".properties || EXCLUDED.properties;

COMMIT;
```

Simplified: the real statements (`graphos.writes`) also bump `graphos_rev`
and record which properties each label carries (`graphos_meta.node_properties`).

The `DISTINCT ON … ORDER BY ctid DESC` collapses duplicate ids within one
batch (last row wins). Without it Postgres rejects the INSERT with
"ON CONFLICT DO UPDATE command cannot affect row a second time".

Edges (`graphos.writes.upsert_edges`, used for every edge
write on every endpoint):

```sql
SET LOCAL synchronous_commit = off;
SET LOCAL enable_hashjoin = off;   -- force index probes on the vertex tables
SET LOCAL work_mem = '256MB';      -- room for the DISTINCT ON sort

INSERT INTO "G"."E" (start_id, end_id, properties)
  SELECT src.id, dst.id, (stg.props)::text::agtype
    FROM (SELECT DISTINCT ON (src_ext, dst_ext) src_ext, dst_ext, props
            FROM _stg_e
           ORDER BY src_ext, dst_ext, ctid DESC) stg
    JOIN "G"."Vsrc" src
      ON src.properties->'"id"' = to_jsonb(stg.src_ext)::text::agtype
    JOIN "G"."Vdst" dst
      ON dst.properties->'"id"' = to_jsonb(stg.dst_ext)::text::agtype
  ON CONFLICT (start_id, end_id)
  DO UPDATE SET properties = "E".properties || EXCLUDED.properties;
```

Without `enable_hashjoin = off` the planner tends to hash-join and
seq-scan the whole source/destination label table for every batch; with
it, the JOIN becomes btree probes on the unique `properties->'"id"'`
index. The hint is `SET LOCAL`, so read paths keep the default planner.

Edges whose endpoints don't resolve fall out of the inner JOIN — the
same "no endpoint → no edge" result a Cypher MATCH-MATCH-MERGE gives. A
separate `EXISTS` probe (a semi-join on the unique index) finds the pairs
whose two ends exist; `skipped` counts the input rows not among them, before
de-duplication.

### Why `to_jsonb(text)::text::agtype` works for the JOIN

agtype scalars store strings as `"value"` (with literal double
quotes). Going `text → jsonb → text` yields the same `"value"`
form, then `::agtype` accepts it. Verified equality:

```sql
SELECT properties->'"id"' = to_jsonb('n000001'::text)::text::agtype
  -- → t (true)
```

### Idempotency contract

The COPY path is safe to call repeatedly with the same payload: the
properties come out the same (each repeat still bumps the revision and
records a change-feed entry).

- Vertex: `ON CONFLICT ((properties->'"id"')) DO UPDATE SET
  properties = old || excluded` — the same result as Cypher
  `SET n += {…}`, which graphos does not use for writes (§5)
- Edge: `ON CONFLICT (start_id, end_id) DO UPDATE …` — the same result
  as Cypher `MERGE r SET r += {…}`, which graphos does not use for
  writes (§5). One edge per (src, dst) per edge type.

Duplicates inside one batch (same `id`, or same src/dst pair for an
edge type) are collapsed to the last row before the `INSERT`, so the
`nodes_upserted` / `edges_upserted` counts, taken from the `INSERT 0 <n>`
command tag, are distinct rows written, not input rows.

Crash safety: with `synchronous_commit = off` a crash between
ack and fsync can lose the last commits; re-sending the batch is safe
because the ON CONFLICT path collapses duplicates into a property merge.

---

## 7. Tuning Postgres for graphos

`graphos-db` runs with (`docker-compose.graphos.yml`):

```yaml
shared_buffers=1GB
effective_cache_size=2GB
work_mem=64MB
maintenance_work_mem=512MB
max_connections=50
max_wal_size=2GB
shared_preload_libraries=age,pg_stat_statements
pg_stat_statements.track=all
log_min_duration_statement=500ms
```

plus `mem_limit: 2g` / `mem_reservation: 512m` and `shm_size: 1g` on the
container (the shared memory lets large aggregate queries, whose segments can
exceed 64 MB, avoid exhausting the default `/dev/shm`). `graphos-api` gets
`mem_limit: 2g`. These defaults start on a laptop with about 4 GB free for
Docker.

Guidance:

- **Raise memory together for multi-million-edge graphs:** `shared_buffers`
  to 4GB, `effective_cache_size` to 6GB, and the `graphos-db` `mem_limit` to
  6g. `shared_buffers` is the single biggest knob for AGE; GIN containment
  scans cache pages aggressively.
- **`graphos-api` memory** matters only during `recompute-communities`,
  which holds the edge list in process: 2 GB covers roughly 3 M edges;
  raise the limit to 4g for 6 M+.
- **`work_mem = 64 MB`** globally is enough for the read paths. The edge
  COPY path raises it to 256 MB with `SET LOCAL` for its own transaction,
  so there is no need to raise it globally for bulk loads.
- **`maintenance_work_mem ≥ 512 MB`** if you rebuild GIN indexes.
- **`max_wal_size ≥ 2 GB`** — bulk-load bursts write a lot of WAL;
  smaller limits trigger frequent checkpoints.
- **`synchronous_commit = off`** is scoped per transaction via
  `SET LOCAL` on the bulk and community-writeback paths. Don't disable it
  globally; other writes still want the durability.
- **Statement timeout.** Every pooled connection runs with a server-side
  `statement_timeout` of `GRAPHOS_STATEMENT_TIMEOUT_S` (default 60 s);
  a cancelled statement answers 504, on write endpoints (`/nodes`,
  `/edges`, `:bulk`, `snapshot-import`) as on reads. The asyncpg pool sets
  no client-side `command_timeout`. The timeout applies per statement, so
  it bounds each statement of a COPY-staged flush (the COPY, the `INSERT`),
  not the flush as a whole. The Cypher console's own pool uses
  `GRAPHOS_CYPHER_TIMEOUT_S` (default 30 s) instead, with a watchdog. Long
  operations raise the limit for their own statements: tenant bootstrap,
  internal schema setup and the startup reload of stored tenants to 1800 s
  (index builds on large labels can take minutes), rule-based edge
  resolution to 1800 s, the Louvain edge stream and graph metrics to 600 s,
  the community back-fill to 300 s per edge label, the dataset export to
  900 s, and vacuum to 3600 s.

---

## 8. Concurrency profile

Measured with `scripts/bench_parallel_rw.py`
(1 writer on the COPY path + 8 readers, 13k nodes + 44k edges, 90 s read
window):

| Query | Idle (writer done) | Active (writer running) | Degradation |
|---|---|---|---|
| `stats` | 28 ms | 89 ms | 3.2× |
| `neighbors_1hop` | 68 ms | 130 ms | 1.9× |
| `induced_edges` | 353 ms | 542 ms | 1.5× |
| `expand_2hop` | 779 ms | 1,390 ms | 1.8× |
| Aggregate p50 | 210 ms | 390 ms | 1.85× |
| Aggregate p95 | 953 ms | 1,894 ms | 2.0× |

Writer **not slowed** by reader contention (no lock conflicts).
1,877 queries served, 0 errors.

Conclusion: AGE handles concurrent reads and writes cleanly under the
COPY bulk path. The ~2× p95 degradation during writes is consistent with
MVCC and GIN page-lock overhead.

---

## 9. Community detection (Louvain)

`POST /v1/{tenant}/recompute-communities` (`maintain` on the tenant; needs the optional
`louvain` extra, otherwise 501) runs in three phases. The response
reports each phase's duration (`edge_stream_ms`, `writeback_ms`, …).

1. **Edge stream** → one SQL scan of `"<tenant>"._ag_label_edge`
   returning `(start_id, end_id, rel_type)` as raw graphids (no agtype
   decoding of the `id` property). Runs with a 10-minute statement budget.
2. **igraph Louvain** → community assignments, in-process, with a fixed
   seed. New clusters inherit the id of their best-overlapping previous
   cluster (Jaccard ≥ 0.5), so a rerun on an unchanged graph produces
   identical ids.
3. **Writeback** → `community_id` on each vertex, written only for
   vertices whose id actually changed, via a COPY-staged UPDATE against
   the inheritance parent `"<tenant>"._ag_label_vertex` (Postgres routes
   each row to its child label table). Then `end_community_id` and
   `start_community_id` are back-filled on every edge label table, in UPDATEs
   of 50,000 vertices each.

For comparison, a per-row `cypher() SET` writeback manages ~42 nodes/s
(215 s for 9k vertices).

The vertex writeback template:

```sql
BEGIN;
SET LOCAL synchronous_commit = off;
CREATE TEMP TABLE _stg_assignments (gid bigint, community_id int)
  ON COMMIT DROP;
-- asyncpg: copy_records_to_table('_stg_assignments', …)
UPDATE "<tenant>"._ag_label_vertex t
   SET properties = t.properties
       || ('{"community_id": ' || s.community_id || '}')::agtype
  FROM _stg_assignments s
 WHERE t.id = (s.gid::text)::graphid;
COMMIT;
```

Output shape: **`community_id` is a node property**, NOT a
separate `:Community` node + membership edge. The read side
(`GET /v1/{tenant}/graph/communities`, `/graph/community-summary`)
aggregates back.

---

## 10. Operational gotchas

- **Unprivileged API role.** The bundled compose runs `graphos-api` as
  `graphos_app` (no superuser), created by
  `services/graphos/deploy/db-init/01-app-role.sh`. Such a role cannot
  `LOAD 'age'`; graphos tries it once per connection, ignores the refusal,
  and relies on `shared_preload_libraries=age`. Init scripts run only on an
  empty data volume, so a volume created before that script needs the role
  created by hand (the SQL is in the script).
- **`pg_advisory_unlock_all`, `RESET ALL` etc. on connection release**
  are asyncpg's normal pool reset — not a bug. graphos re-applies
  `search_path` on every acquire for that reason.
- **The calling client stalls while graphos backends show no active
  statement** → the problem is client-side (e.g. an exhausted HTTP
  connection pool), not AGE. Put timeouts on every client call.
- **Dropping an index that reads depend on** sends those reads to
  sequential scans until it is rebuilt; schedule index rebuilds in a
  quiet window.
- **Audit logging** emits one structured JSON line per write request (one
  per `:bulk` POST, not per row; message `write`), and one per large read
  such as an export (message `read`), with `method`, `path`, `tenant`,
  `consumer`, `args_hash`, `status` and `elapsed_ms`. Turn it off with
  `GRAPHOS_AUDIT_ENABLED=false` if it is unwanted.
- **Generic plans (PostgreSQL 18).** asyncpg prepares each statement on a
  connection; after five runs PostgreSQL may keep a generic plan, made
  without the values. For a scan on two property filters PostgreSQL 18 kept
  one that walked the id index and dropped 34,702 rows to return 100
  (50 ms became about 1 s). graphos's connections set
  `plan_cache_mode = force_custom_plan`.
- **Property filters are priced as cheap (PostgreSQL 18, compact tables).**
  A filter on `properties` converts each row (agtype to text to jsonb), which
  the planner counts as one cheap operator. On a compact table (a fresh load,
  or a restore) it walked the id index in order for a selective filter:
  649 matches of 100,000 rows in 334 ms, against 28 ms through the property
  indexes and a sort. graphos's filtered `nodes:scan` raises
  `cpu_operator_cost` to 0.025 in its own transaction.
- **A cursor over an unindexed temporary table.** PostgreSQL plans a cursor
  for its first rows; joined to a temporary table without an index, it
  compared every edge with every vertex (76.6 s for the first 2,000 rows,
  where the same statement runs in 2.1 s). Index temporary tables a cursor
  joins.
- **More than 128 graphs on AGE 1.8: a warning per write.** AGE 1.8 keeps a
  shared-memory version counter per graph, to invalidate its in-memory graph
  (the one variable-length patterns use), for at most 128 graphs since the
  server started; slots are not reused when a graph is dropped. Past that,
  each write statement to another graph logs `AGE: graph version counter
  table full (128 graphs)` and AGE falls back to its older check. graphos
  never uses that in-memory graph, so nothing else changes: on a server with
  many tenants (or after a test suite) expect the warning in PostgreSQL's
  log until a restart. The statement-level trigger behind it
  (`_age_cache_invalidate`) is on label tables AGE 1.8 creates; tables restored
  from a 1.6 dump get it from the move's import and from graphos-api at start
  ([upgrade guide](upgrade-postgresql-18.md#age-18s-cache-trigger)).
- **After a restore, gather statistics.** A dump carries no planner
  statistics; run `vacuumdb --analyze-only --jobs N` before traffic (the
  compose's PostgreSQL 18 import does).
- **`pg_stat_activity`** shows a backend as `active` only while a
  statement runs; between the statements of one transaction (the
  `CREATE TEMP TABLE`, `COPY` and `INSERT` of a staged flush) it is briefly
  `idle in transaction`. That's expected, not a leak.

---

## 11. Upstream status

Checked 2026-10-15 against `apache/age`. The bundled database is AGE 1.8.0
on PostgreSQL 18. AGE 1.6.0 on PostgreSQL 16, which has no AGE release after
1.6.0 (the fixes below ship in 1.8.0, released for PostgreSQL 15 and 18), is no
longer supported: graphos-api refuses to start on it. CI runs the server's tests on
1.8 only.

| Pitfall | Upstream |
|---|---|
| Cypher `SET` on an edge table with extra columns segfaults (§5) | No crash from 1.8.0 (apache/age#2458); the write is refused (the extra columns would be NULL): graphos still writes with SQL only |
| Variable-length patterns and `vertex_stats()`: the mutex left locked (§5) | Fixed in 1.8.0 (apache/age#2433); verified: the session stays usable |
| Variable-length patterns and `vertex_stats()`: the column-count check (§5) | Open (apache/age#2583): the console refuses them; the API never needs them |
| Cypher `SET` / `REMOVE` / `DELETE` carried out in a read-only transaction (§5) | Stopped from 1.8.0 (verified); graphos's console refuses write clauses on every version |
| No interrupt checks in the variable-length search (§5) | Added in 1.8.0 (apache/age#2430); still none in the graph load |
| `>=` and `>` swapped in `agtype_ops_btree` (§4) | Fixed on master (apache/age#2577), in no release yet; reported as apache/age#2587 |
| `ON CREATE SET` / `ON MATCH SET` rejected (§1) | Supported from 1.8.0 (apache/age#2347) |
| Multi-label patterns (§1, §3) | Open (apache/age#234) |
| `SET n += $param`, `SET n += row.props` (§1) | Not supported |
| Chained `MERGE` slow (§1) | Open (apache/age#2177) |
| GIN key-exists `?` missing rows (§4) | No matching issue found; apache/age#2322 reports missing rows with a GIN index |

Moving from AGE 1.6 to 1.8 therefore means moving from PostgreSQL 16 to 18
as well: the compose does it by itself with a dump and a restore
([upgrade guide](upgrade-postgresql-18.md)). `pg_upgrade` does not fit: it
refuses AGE's `ag_graph.namespace` (type `regnamespace`), and AGE's
`age_prepare_pg_upgrade()` exists from 1.8 only. A restore gives every schema
a new OID, which AGE's catalogue records: point `ag_graph.graphid` and
`ag_label.graph` at `ag_graph.namespace`, as `age_finish_pg_upgrade()` does.
A restore also runs none of AGE's upgrade scripts, so the label tables of a
1.6 dump lack the `_age_cache_invalidate` trigger 1.8 gives its own: the
import adds it, as AGE's 1.7.0 to 1.8.0 script does.

---

## 12. Measured on PostgreSQL 18 / AGE 1.8

Measured 2026-10-15 on a laptop (8 cores, Docker VM with 16 vCPUs and 31 GB), with the image's
default PostgreSQL settings (128 MB `shared_buffers`) on both databases, one graphos worker, on
a 251,246-node, 940,144-edge graph (24 labels; computed metrics and communities), against the
same graph on PostgreSQL 16.10 / AGE 1.6.0 and on its PostgreSQL 18.6 / AGE 1.8.0 restore.

**A mix of 12 reads** (node by id, neighbours, expand 2 hops, paths ≤ 4 hops, search, a
filtered `nodes:scan` page, community ids, a label histogram (since replaced by `nodes/aggregate`), four console queries), 30 warm runs
each, no think time:

| | PostgreSQL 16 / AGE 1.6 | PostgreSQL 18 / AGE 1.8 |
|---|---:|---:|
| 1 user, 360 requests | 85 s | **67 s** |
| 5 users, 360 requests | 57 s | **35 s** |
| paths ≤ 4 hops, p50, 1 / 5 users | 305 / 1233 ms | **154 / 529 ms** |
| neighbours (200), p50, 5 users | 784 ms | **350 ms** |
| console aggregate over a whole label, p50, 1 / 5 users | 1035 / 2709 ms | **621 / 1397 ms** |
| errors | 0 of 840 | 0 of 840 |

PostgreSQL 18's tables were compact after the restore, while the PostgreSQL 16 copy carried
about 3× dead space; that favours PostgreSQL 18.

**Dataset export and import** (PostgreSQL 18, under a five-user read load with 0.5 s think
time; 3,842 concurrent requests, all answered):

| | Time | graphos memory, peak |
|---|---:|---:|
| export, every node and edge with computed values, gzip (32 MB) | 71 s | 105 MiB |
| import into an empty tenant (`mode=empty`) | 470 s | 158 MiB |

The copy's content checksum matched the source's. An import writes about 2,500 rows/s here: each
row goes through the tenant's indexes (13 property, trigram and search indexes on this graph), and
the edge community columns are restored after the edges.

**Recompute** (PostgreSQL 18; a first run on fresh data writes every row, later runs only what
changed):

| Graph | Metrics (degree, PageRank, components) | Louvain |
|---|---:|---:|
| the graph above, groups unchanged since the last run | 13.8 s | 46 s (144 communities) |
| synthetic, 270,000 nodes / 1.0M edges, first run | 51 s | 276 s |
| synthetic, 810,000 nodes / 3.0M edges, first run | 152 s | 1,373 s |

Betweenness is skipped above `GRAPHOS_BETWEENNESS_MAX_NODES` (20,000). Metrics grow linearly with
the graph; Louvain's first run is mostly its write-back (every node's `community_id` and both
community columns of every edge), which also leaves dead rows behind until autovacuum.

**Import, isolated** (the same 1.19M-row export into an empty tenant): 447 s with no load, 470–488 s
with five concurrent readers, 507 s with the tenant's property and search indexes left out; the
time is the import path itself (about 2,500 rows/s here), not the load or the declared indexes.

**`VACUUM FULL`** of all 24 label tables (748 MB) through `POST /v1/db/vacuum`: 309 s; reads of a
table wait while it is rewritten (up to 21.6 s for the largest), other tables stay available.
`pg_repack` is not in the `apache/age` image.

**Moving PostgreSQL 16 to 18** (dump and restore, as the compose does): a 9.5 GB database with
five tenants in 12 minutes, 4.3 GB afterwards; a 2 GB tenant's restore in 72 s plus 36 s of
statistics.

**Fixed after these measurements showed them:**

| | Before | After |
|---|---:|---:|
| full dataset export (the cursor over an unindexed table, §10) | not done after 33 min | 71 s |
| a filtered `nodes:scan` on PostgreSQL 18 (filter costing, §10) | 350 ms | 37 ms |
| the same scan after its fifth run on a connection (generic plan, §10) | about 1 s | 50 ms |

## 13. References

- AGE source — `apache/age` on GitHub
- apache/age#2177 — performance of upserting 10k+ edges with `MERGE`:
  https://github.com/apache/age/issues/2177
- AGE manual — Importing Graph from Files:
  https://age.apache.org/age-manual/master/intro/agload.html
- AGE manual — MERGE clause:
  https://age.apache.org/age-manual/master/clauses/merge.html
- In this project: [`graph_age_practices.md`](./graph_age_practices.md)
  (write-path templates), `scripts/bench_bulk_write.py`,
  `scripts/bench_parallel_rw.py`

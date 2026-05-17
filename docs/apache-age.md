# Apache AGE — practices and lessons learned

Hands-on notes from running Apache AGE 1.6.x on PostgreSQL 16
(`apache/age:release_PG16_1.6.0`) under graphos's write and read paths.
For the write-path templates in more detail see
[`graph_age_practices.md`](./graph_age_practices.md).

---

## 1. Cypher syntax — what AGE 1.6 rejects

| Pattern | Status | Workaround |
|---|---|---|
| `SET n += $param` (parametric map) | ❌ rejected | Inline as Cypher map literal: `SET n += {k: 'v', …}` |
| `SET n += row.props` (UNWIND projection) | ❌ rejected | Same — inline literal, one `cypher()` call per row |
| `ON CREATE SET … ON MATCH SET …` | ❌ rejected at parse | Always plain `SET` after `MERGE` |
| Multi-label `(:L1:L2 …)` in `CREATE/MERGE/MATCH` | ❌ parse error | Postgres `INHERIT` between label tables (see §3) |
| Multiple `;`-separated statements inside one `cypher(...)` call | ❌ rejected | One statement per cypher() call |
| `WITH …`-chained `MERGE`s for batched writes | ⚠️ super-linear | Never chain >1 MERGE — issue separate cypher() calls or use COPY (see §5) |
| Cypher `SET` on a label whose table has extra (non-AGE) columns | 💥 backend segfault (AGE 1.6.0) | Write that label with plain SQL only (see §5) |

The right mental model: AGE is a **Postgres extension that transpiles
openCypher to SQL**. Anything that doesn't have a clean SQL lowering
(parameterised maps, chained writes, conditional set clauses) is
either rejected at parse time or compiled to something slow.

graphos writes no rows through Cypher. Every node and edge upsert, on
every write endpoint, goes through the COPY-staged SQL path of §6:
`graphos.cypher.upsert_nodes_inline` delegates to
`bulk_upsert_nodes_via_copy`, and `upsert_edges_inline` to
`bulk_upsert_edges_via_copy`. §5 gives the two reasons (throughput and a
backend segfault). Cypher remains in use for some reads and for node
deletes (`DETACH DELETE`, verified unaffected by the segfault). The rows
above about inlined map literals and per-row `cypher()` calls describe what
a Cypher write needs on AGE 1.6; graphos does not write that way.

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
| `GIN (properties)` per vertex label | Serves Cypher `MATCH (n:L {k: v})` (containment `@>`) |
| `UNIQUE ((properties->'"id"'))` per vertex label | Arbiter for the `ON CONFLICT` of every node write; serves direct-SQL id lookups and the edge-endpoint JOIN |
| btree on `community_id` (text extract) per vertex label | Community endpoints, community-scoped expansion |
| trigram GIN on `name` per vertex label | `/graph/search` (case-insensitive `ILIKE '%q%'`; `%` and `_` in `q` are escaped, so `q` matches literally) |
| `UNIQUE (start_id, end_id)` per edge label | Arbiter for the `ON CONFLICT` of every edge write |
| `btree (start_id, end_id)` per edge label | Outgoing-edge lookups on read paths (kept alongside the unique one) |
| `btree (end_id)` per edge label | Incoming-edge lookups (`WHERE end_id IN (…)`) |
| partial `btree (end_community_id)` per edge label | Community-scoped BFS |

Two different access paths, two different indexes:

- **Cypher `MATCH (n:L {id: X})` compiles to
  `WHERE properties @> '{"id":"X"}'::agtype`** — the GIN containment
  operator. It does *not* lower to `properties->'"id"' = …`, so the btree
  unique index is **not** used by Cypher MATCH. GIN on `properties` is
  what keeps Cypher reads and deletes by id off a sequential scan.
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

---

## 5. Why every write is COPY-staged SQL

graphos has one write path: the COPY-staged `INSERT … ON CONFLICT` of §6.
There is no flag and no fallback. Two findings led there.

**Reason 1 — throughput.** Measured with `scripts/bench_bulk_write.py`
on a 13k-node + 44k-edge synthetic dataset (graphos-db with 4 GB
shared_buffers, single host), comparing per-row Cypher with the COPY path:

| Approach | Mechanism | Edge throughput |
|---|---|---|
| Per-row `cypher() MERGE` (not used by graphos) | One parse+plan per row, props inlined as a literal | **31 edges/s** |
| **COPY-staged `INSERT … ON CONFLICT`** (current) | One binary COPY into a `TEMP TABLE … ON COMMIT DROP`, then one `INSERT … SELECT … ON CONFLICT` per label / edge type | **1,153–4,840 edges/s** |

That is ≈37–156× in favour of the COPY path on this benchmark.

**Reason 2 — stability.** A Cypher write into a label table with extra
columns crashes the server; see the pitfall below.

Every write endpoint uses that path: `POST /v1/{tenant}/graph/nodes`,
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

### Pitfall: extra columns on a label table + Cypher `SET` → backend segfault

AGE builds the tuple for a Cypher write from its own idea of the label
table's columns. If a label table carries an **extra column** that AGE did
not create, AGE 1.6.0 segfaults the Postgres backend on any Cypher `SET` or
`MERGE … SET` that writes a row of that label, new or existing. The
postmaster then restarts the whole server and every connection drops.

graphos label tables can carry such columns in two ways: bootstrap adds an
`end_community_id` column to every edge table (§9, community-scoped
traversal), and registration `generated_columns` add columns to any vertex
or edge label. Verified live on AGE 1.6.0. graphos therefore issues **no
Cypher writes**: every node and edge write is the SQL `INSERT … ON CONFLICT
… DO UPDATE SET properties = properties || EXCLUDED.properties` of §6,
which never enters AGE's tuple-building code and keeps the same contract
(additive property merge, missing endpoints counted as skipped). As a
result generated columns on vertex labels are safe: SQL writes fill
them correctly (verified).

The SQL path also sidesteps an AGE 1.6 quirk: `SET r += {…}` on an
already-merged edge intermittently raised `Entity failed to be updated: N`
even though the MERGE succeeded.

---

## 6. COPY-staged bulk upsert — the canonical pattern

Vertices (`graphos.cypher.bulk_upsert_nodes_via_copy`, used for every node
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

The `DISTINCT ON … ORDER BY ctid DESC` collapses duplicate ids within one
batch (last row wins). Without it Postgres rejects the INSERT with
"ON CONFLICT DO UPDATE command cannot affect row a second time".

Edges (`graphos.cypher.bulk_upsert_edges_via_copy`, used for every edge
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
same "no endpoint → no edge" result a Cypher MATCH-MATCH-MERGE gives. They
are counted as `skipped` with a separate `NOT EXISTS` probe (an
anti-semi-join that also uses the unique index); that count is of input
rows, before de-duplication.

### Why `to_jsonb(text)::text::agtype` works for the JOIN

agtype scalars store strings as `"value"` (with literal double
quotes). Going `text → jsonb → text` yields the same `"value"`
form, then `::agtype` accepts it. Verified equality:

```sql
SELECT properties->'"id"' = to_jsonb('n000001'::text)::text::agtype
  -- → t (true)
```

### Idempotency contract

The COPY path is safe to call repeatedly with the same payload:

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
  `/edges`, `:bulk`, `snapshot-import`) as on reads. It is the only
  per-statement limit:
  the asyncpg pool sets no client-side `command_timeout`. The timeout
  applies per statement, so it bounds each statement of a COPY-staged
  flush (the COPY, the `INSERT`), not the flush as a whole. Long operations raise it for their own statements:
  tenant bootstrap, internal schema setup and the startup reload of stored
  tenants to 1800 s (index builds on large labels can take minutes),
  Louvain stages to 600 s / 300 s, rule-based edge resolution to 1800 s,
  and vacuum to 3600 s.

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

`POST /v1/{tenant}/recompute-communities` (admin; needs the optional
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
   each row to its child label table). Then `end_community_id` is
   back-filled on every edge label table, one UPDATE per label.

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
  per `:bulk` POST, not per row) with `method`, `path`, `tenant`,
  `consumer`, `args_hash`, `status` and `elapsed_ms`. Turn it off with
  `GRAPHOS_AUDIT_ENABLED=false` if it is unwanted.
- **`pg_stat_activity`** shows a backend as `active` only while a
  statement runs; between the statements of one transaction (the
  `CREATE TEMP TABLE`, `COPY` and `INSERT` of a staged flush) it is briefly
  `idle in transaction`. That's expected, not a leak.

---

## 11. References

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

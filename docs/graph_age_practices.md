# Apache AGE — practices for high-throughput, idempotent writes

A working catalogue of what AGE 1.6.x does and does not accept, what
costs are surprising, and the concrete templates graphos uses on the
write path (`graphos.cypher`, `graphos.bootstrap`, `graphos.api.bulk`).
Verified against AGE 1.6.0 on PostgreSQL 16. For tuning, concurrency
and community-detection notes see [`apache-age.md`](./apache-age.md).

## 1. AGE Cypher acceptances and rejections

What Neo4j-style Cypher accepts that AGE 1.6 does not:

| Pattern | AGE 1.6 |
|---|---|
| `SET n += $param` (parametric map) | rejected |
| `SET n += row.props` (UNWIND projection) | rejected |
| `ON CREATE SET … ON MATCH SET …` | rejected — parse error |
| Multi-label syntax `(:L1:L2 …)` in CREATE / MERGE / MATCH | rejected — parse error at second `:` |
| `;` between cypher statements inside one `cypher(...)` call | rejected — one statement per call |
| Cypher `SET` on a label whose table has extra (non-AGE) columns | backend segfault (AGE 1.6.0) |

Because of the last row (and the throughput gap in §3), graphos writes
no rows through Cypher: every write is SQL in `graphos.writes`, and every
node and edge upsert is the COPY-staged SQL path of §3. Cypher is still used
for some reads. What follows is what graphos does,
plus the workaround a per-row Cypher write would need:

- **Per-row Cypher writes must inline every property map as a literal**
  (`SET n += {k: 'v', …}`), one `cypher()` call per row. That is the only
  way around the first two rows; single quotes and backslashes in values
  must be escaped, keys that aren't plain identifiers wrapped in
  backticks, and the MERGE key (`id`) kept as a real parameter. graphos
  does not write this way.
- **Multi-label = postgres table inheritance.** Each child label's table
  inherits from the parent's; `MATCH (n:Parent {id: X})` finds rows in
  any inheriting child via postgres's inheritance scan.
  Writes must target the child label — `MERGE` against the parent
  inserts a new row into the parent table instead of finding the
  inherited child.
- **All rows are written only through SQL.** When an AGE label table has
  any column beyond AGE's own (bootstrap's `end_community_id` and
  `start_community_id` on every edge table, or a declared generated column on a vertex
  label), AGE 1.6 segfaults the Postgres backend on a Cypher `SET` /
  `MERGE … SET` that writes a row into that table (verified live; the
  server restarts and every connection drops). `graphos.writes`
  therefore upserts nodes and edges with SQL only (§3): `INSERT …
  ON CONFLICT ((properties->'"id"'))` for nodes and `ON CONFLICT
  (start_id, end_id)` for edges, both `DO UPDATE SET properties =
  properties || EXCLUDED.properties`, for every write on every endpoint.
  SQL writes fill generated columns correctly, so generated columns
  on vertex labels are safe. This also avoids the intermittent
  ``Entity failed to be updated: N`` from `SET r += {…}`.
  See [`apache-age.md` §5](./apache-age.md#5-why-every-write-is-copy-staged-sql).

## 2. Indexing — the load-bearing optimisation

`create_vlabel` does **not** create a usable index on the consumer's
identity property. AGE stores per-row data as
``(id graphid, properties agtype)``; the consumer's ``id`` lives inside
``properties``. There are two ways to look a row up by that id, and each
needs its own index:

| Access path | Compiles to | Index |
|---|---|---|
| Cypher `MATCH (n:Label {id: X})` / `MERGE` | ``WHERE properties @> '{"id":"X"}'::agtype`` | GIN on `properties` |
| Direct SQL (`ON CONFLICT`, id lookups, edge-endpoint JOIN) | ``properties->'"id"' = '"X"'::agtype`` | unique btree on `(properties->'"id"')` |

Cypher never lowers the property-map match to the `->` form, so the btree
doesn't help Cypher, and GIN can't act as an `ON CONFLICT` arbiter — hence
both. Bootstrap creates them for every label:

```sql
CREATE INDEX IF NOT EXISTS "<label>_properties_gin"
  ON "<graph>"."<label>" USING gin (properties);

CREATE UNIQUE INDEX IF NOT EXISTS "<label>_id_unique"
  ON "<graph>"."<label>" ((properties->'"id"'));

-- per edge label
CREATE UNIQUE INDEX IF NOT EXISTS "<edge>_endpoints_unique"
  ON "<graph>"."<edge>" (start_id, end_id);
CREATE INDEX IF NOT EXISTS "<edge>_endpoints_btree"
  ON "<graph>"."<edge>" USING btree (start_id, end_id);
CREATE INDEX IF NOT EXISTS "<edge>_end_id"
  ON "<graph>"."<edge>" USING btree (end_id);
```

Note the inner double quotes in `properties->'"id"'`: the key is an
agtype string literal. Queries must spell the expression exactly as the
index does, or the planner won't use it.

Measured impact of the GIN index (4k-row table):

| Query | Without index | With GIN | Speedup |
|---|---|---|---|
| `MATCH (n:Label {id: X})` | ~6 ms (seq scan) | ~0.4 ms (bitmap heap scan via GIN) | ~15× |

The gap widens linearly with table size. Graphos's tenant bootstrap
creates these indexes automatically; see `graphos.bootstrap.bootstrap_tenant`.
Because the unique index is per label table and Postgres unique indexes
are not inherited, the same id may exist once per label.

## 3. Bulk-write patterns and anti-patterns

### Per-row MERGE inside one transaction — not used

The simplest correct Cypher shape. graphos does **not** use it for nodes
or edges: it is slow, and it crashes the backend on any label table
carrying extra columns (§1). Each row is a separate `cypher(...)`
invocation; a surrounding transaction keeps each batch atomic.

```sql
SELECT * FROM cypher('<graph>', $$
  MERGE (n:Label {id: $id})
  SET n += { k: 'v', n: 42 }
$$, $1::agtype) AS (_ agtype)
```

The edge form is `MATCH (a:Src {id: $src_id}) MATCH (b:Dst {id: $dst_id})
MERGE (a)-[r:TYPE]->(b) SET r += {…}`. Cost is dominated by the per-call
parse + plan: on `scripts/bench_bulk_write.py` (13k nodes, 44k edges)
it sustains **31 edges/s**.

### Chained MERGE inside one `cypher()` call — DO NOT USE

```cypher
MERGE (n0:L {id: 'a'}) SET n0 += {…}
WITH 1 AS _
MERGE (n1:L {id: 'b'}) SET n1 += {…}
WITH 1 AS _
…
```

Parses and executes correctly, but AGE's WITH-chain handling between
MERGE clauses scales super-linearly in chain length. A 200-row chain
took 30–90 seconds against a ~50k-row label; the per-row form for the
same 200 rows finished in ~200 ms.

### UNWIND a list of maps — DO NOT USE for SET

```cypher
UNWIND [{id:'a', props:{x:1}}, …] AS row
MERGE (n:L {id: row.id})
SET n += row.props      -- ❌ rejected
```

UNWIND itself works; only the `SET n += row.props` projection is
rejected. There's no way around this within Cypher; the property map
must be a literal at parse time.

### Direct SQL on per-label tables — the COPY path (graphos's only write path)

Bypassing Cypher and writing directly to the AGE label tables removes
the per-row parse/plan cost. Direct `INSERT` rows are visible to Cypher
`MATCH` because AGE reads the same tables, and the `graphid` column
default mints ids.

graphos's implementation (`graphos.writes.upsert_nodes` /
`upsert_edges`) stages each batch with binary COPY and
upserts against the unique indexes from §2:

```sql
-- vertices
INSERT INTO "<graph>"."<label>" (properties)
  SELECT (jsonb_set(props, '{id}', to_jsonb(ext_id)))::text::agtype
    FROM (SELECT DISTINCT ON (ext_id) ext_id, props
            FROM _stg_n ORDER BY ext_id, ctid DESC) deduped
  ON CONFLICT ((properties->'"id"'))
  DO UPDATE SET properties = "<label>".properties || EXCLUDED.properties;

-- edges: resolve external ids to graphids by joining the vertex tables
INSERT INTO "<graph>"."<edge>" (start_id, end_id, properties)
  SELECT src.id, dst.id, (stg.props)::text::agtype
    FROM (SELECT DISTINCT ON (src_ext, dst_ext) src_ext, dst_ext, props
            FROM _stg_e ORDER BY src_ext, dst_ext, ctid DESC) stg
    JOIN "<graph>"."<src_label>" src
      ON src.properties->'"id"' = to_jsonb(stg.src_ext)::text::agtype
    JOIN "<graph>"."<dst_label>" dst
      ON dst.properties->'"id"' = to_jsonb(stg.dst_ext)::text::agtype
  ON CONFLICT (start_id, end_id)
  DO UPDATE SET properties = "<edge>".properties || EXCLUDED.properties;
```

Lessons baked into that template:

- `||` on agtype gives the same key-wise merge as Cypher `SET n += {…}`.
- `DISTINCT ON … ctid DESC` collapses duplicate keys within one batch
  (last wins); otherwise `ON CONFLICT DO UPDATE` fails with "cannot affect
  row a second time". graphos reports `nodes_upserted` / `edges_upserted`
  from the `INSERT 0 <n>` command tag, so the counts are distinct rows
  written, not input rows.
- The edge transaction sets `enable_hashjoin = off` (and `work_mem =
  256MB`) with `SET LOCAL`, so the JOIN probes the unique btree instead of
  hash-joining a full scan of each vertex table per batch.
- Edges are unique per `(start_id, end_id)` within an edge type — the
  same contract a Cypher `MERGE` gives. Input rows whose endpoint does
  not resolve are counted as skipped.

On the same benchmark this path sustained **1,153–4,840 edges/s**
(≈37–156× the per-row path). That gap, together with the segfault in §1,
is why every node and edge write uses it, on every endpoint (`/nodes`,
`/edges`, `nodes:bulk`, `edges:bulk`, `snapshot-import`); there is no
flag. The upserts need bootstrap's unique `<Label>_id_unique` and
`(start_id, end_id)` indexes as `ON CONFLICT` arbiters. graphos
re-bootstraps every stored tenant at startup, so a tenant created before
those indexes existed gets them on the next restart.

When taking this path, invariants that Cypher enforced (label
membership, endpoint existence) become the writer's responsibility —
graphos handles them by validating labels against the registered tenant
and by the inner JOIN on endpoints.

### Bulk-load via AGE's built-in functions

AGE ships server-side bulk loaders:

```sql
SELECT load_labels_from_file('<graph>', '<label>', '<path>');
SELECT load_edges_from_file ('<graph>', '<label>', '<path>');
```

These read CSV from a server-side filesystem path. Useful when an
operator can stage CSVs into a postgres-visible volume; less useful
across an HTTP boundary. graphos does not use them.

### Statement timeout

Every pooled connection runs with a server-side `statement_timeout` of
`GRAPHOS_STATEMENT_TIMEOUT_S` (default 60 s); a cancelled statement
answers 504, on the write endpoints (`/nodes`, `/edges`, `:bulk`,
`snapshot-import`) as on reads. It is the only per-statement limit — the pool sets no
client-side `command_timeout`. It applies per statement — each COPY and
each COPY-path `INSERT` — so a 500-row flush stays well inside it on an
indexed tenant. If bulk writes start timing out, check for
a missing index before raising the limit. Tenant bootstrap (index builds)
runs under its own 1800 s limit.

## 4. Graph names and identifier safety

`create_graph` accepts mixed-case names. AGE quotes the resulting
schema identifier (`"Acme"` rather than `acme`), so CRUD
operations keep their case end-to-end.

Graphos's `TenantSpecBody` constrains tenant names to
``^[A-Za-z][A-Za-z0-9_]{0,62}$`` and labels to
``^[A-Z][A-Za-z0-9_]{0,62}$`` — postgres-identifier-safe AND
SQL-string-literal-safe (no quotes, no special chars). The regex
is the SQL-injection guard for the `f"create_graph('{name}')"` and
`create_vlabel` / `create_elabel` templates; relaxing it requires also
escaping `'` in the name.

## 5. Choosing the right write path

| Path | Measured edge throughput | Used by |
|---|---|---|
| Per-row `cypher()` MERGE | 31 edges/s | not used |
| Chained MERGE in one `cypher()` | super-linear slowdown | never |
| COPY into staging + `INSERT … ON CONFLICT` | 1,153–4,840 edges/s | every node and edge write, on every endpoint |

Numbers are from `scripts/bench_bulk_write.py` (13k nodes, 44k edges,
single host). There is no choice to make: the COPY-staged SQL path is
graphos's only write path, for throughput and because Cypher writes crash
the backend on label tables with extra columns (§1). The contract surface
is thinner on the COPY path (AGE's invariants shift to the writer), which
graphos covers by validating labels and by the endpoint JOIN.

## References

- apache/age#2177 — Performance issue when upserting 10k+ edges
  using MERGE:
  https://github.com/apache/age/issues/2177
- AGE manual — Importing Graph from Files:
  https://age.apache.org/age-manual/master/intro/agload.html
- AGE manual — MERGE clause:
  https://age.apache.org/age-manual/master/clauses/merge.html

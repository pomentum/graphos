# Reading graphos's data without graphos

graphos keeps every tenant as an ordinary [Apache AGE](https://age.apache.org/) graph in
ordinary PostgreSQL tables. Nothing is locked in: AGE's Cypher and plain SQL read the data with
no graphos process running, and the data can be moved to a plain AGE graph, or anywhere else,
from a dataset export. This page is for a DBA, a report tool, or a team leaving graphos.

## The layout

- **One AGE graph per tenant**, in a PostgreSQL schema named after the tenant.
- **One table per label and per relationship type**, inheriting AGE's `_ag_label_vertex` or
  `_ag_label_edge`:
  - vertex tables: `id graphid`, `properties agtype`, plus graphos's `graphos_rev bigint`;
  - edge tables: `id`, `start_id`, `end_id`, `properties`, plus `graphos_rev`,
    `start_community_id text` and `end_community_id text`.
- **Label hierarchy** is PostgreSQL table inheritance: reading a parent label's table includes
  its children's rows (as Cypher `MATCH (n:Parent)` does).
- **External ids** are `properties.id`: the key graphos's API uses. AGE's `graphid` is internal.

What is graphos's own, and safe to ignore:

| Item | Where | What it is |
|---|---|---|
| `graphos_rev` | every label table | revision counter for ETags |
| `start_community_id`, `end_community_id` | every edge table | copies of the ends' `community_id`, for community-scoped walks |
| `graphos_degree`, `graphos_pagerank`, `graphos_component`, … and `community_id` | inside `properties` | computed metrics and groups: plain properties to any reader |
| `graphos_meta.*` | its own schema | registrations (`tenants.spec`), users, grants, sessions, signing keys, saved queries, the change feed, the community map, jobs |
| indexes such as `<Label>_id_unique`, GIN on `properties`, trigram on `name` | per label table | graphos's lookups; declared property and search indexes |

## Reading it

With AGE's Cypher (no graphos involved):

```sql
LOAD 'age';                                  -- or shared_preload_libraries = age
SET search_path = ag_catalog, "$user", public;
SELECT * FROM cypher('sales', $$
  MATCH (c:Customer {id: 'C-42'})-[:BOUGHT]->(p:Product) RETURN c.name, p.name LIMIT 10
$$) AS (customer agtype, product agtype);
```

With plain SQL:

```sql
SELECT properties::text::jsonb ->> 'name' FROM "sales"."Customer" LIMIT 10;
```

Two limits while the data sits in graphos's tables:

- **Variable-length patterns** (`-[*1..3]->`) and `vertex_stats()` fail: AGE's loader refuses
  tables with more than its own columns (apache/age#2583). On AGE 1.8 the query just errors; on
  AGE 1.6 a second attempt in the same session hangs (see [apache-age.md](apache-age.md) §5).
  Write the hops out, or move the data to a plain graph (below).
- **Do not write with Cypher.** AGE does not keep graphos's columns up (AGE 1.6 crashes on an
  edge `SET`; AGE 1.8 refuses the writes). Write through graphos's API, or use the plain copy.

For people who read this way regularly, give them a read-only role:
[installation §8, Direct SQL readers](installation.md#direct-sql-readers) (needs AGE 1.8).

## Taking it out

- **Everything, as it is:** a database dump (`pg_dump -Fc`) carries every tenant, graphos's
  state and AGE's catalogue. It restores into any PostgreSQL with the same AGE major (or PostgreSQL
  18 with AGE 1.8 from 16 with 1.6), followed by AGE's catalogue fix: see
  [upgrade-postgresql-18.md](upgrade-postgresql-18.md), "A database you run yourself".
- **One tenant, as data:** the dataset export, `GET /v1/{tenant}/graph/snapshot`
  (the computed metrics and `community_id` are included unless `?computed=false`; `?gzip=true`
  compresses; `labels` and `types` narrow it). One JSON object per line:
  - first a `header` line: the format, the counts and the tenant's schema;
  - then every node: `{_kind: "node", id, label, properties}`;
  - then every edge: `{_kind: "edge", src_id, src_label, dst_id, dst_label, type, properties}`;
  - last an `end` line with what was written (a file without it was cut off).

  Any loader can map it to another graph store or to tables.
- **The tenant's schema alone:** `GET /v1/tenants/{name}/spec`, a JSON document that is itself a
  registration.

What a dataset export does not carry:
- revisions;
- users, grants, sessions and signing keys;
- saved console queries;
- the community map;
- the change feed.

Rebuild users and grants in whatever replaces graphos. The saved queries are in
`graphos_meta.saved_queries` (`tenant`, `name`, `definition` with the query and its parameters,
`created_by`, `updated_at`). Read them there, or
through `GET /v1/{tenant}/cypher/saved` while the console is on.

## Into a plain AGE graph

[`tools/export/ndjson_to_age.py`](../tools/export/ndjson_to_age.py) loads a dataset export into a
**new AGE graph with only AGE's own columns**, without graphos. Variable-length patterns work on
it.

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8080/v1/sales/graph/snapshot?computed=true&gzip=true" -o sales.ndjson.gz
pip install asyncpg
python tools/export/ndjson_to_age.py sales.ndjson.gz sales_plain \
  --dsn postgresql://user:password@host:5432/db
```

- **What it builds:** one vertex label per node label and one edge label per relationship type;
  AGE assigns new graph ids; node `properties` keep the external `id`.
- **Limit:** it holds the file in memory while loading, so budget about 1 GB per million rows.

Tested 2026-10-15 on AGE 1.8.0 / PostgreSQL 18.6, with a 251,246-node, 940,144-edge graph:
- the content checksums of the source tenant and the plain graph matched, node by node and edge
  by edge (label, ends and properties);
- the plain tables have 2 and 4 columns;
- a `-[:R*1..2]->` query answered there, where it fails on graphos's own tables.

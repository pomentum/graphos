# Changelog

Server (`graphos-api`, image `ghcr.io/pomentum/graphos-api`) and client (`graphos-client`)
are released together under one version.

## Unreleased

### Added

- **Ranked search through named search indexes.** A tenant declares `search_indexes` at
  registration: a name, the vertex labels (or `["*"]`, every label now and later), up to 8 string
  properties, and an analyzer. `GET …/graph/search?index=<name>` returns hits ordered by a `score`
  in [0, 1] with the property that `matched`; `fuzzy` and `min_score` tune it.
  - `identifier` (names, identifiers, paths): camelCase, acronyms and punctuation are word breaks,
    so `parseHTTPResponse` is found by `parse http response` and by `parsehttpresponse`; exact >
    prefix > contained > similar
    words, which catch most typos; `fuzzy=true` adds misspellings when nothing matches literally.
  - `text` (prose): Postgres full-text search with stemming for `language` and web-search syntax.

  Each (label, property) gets its GIN indexes (two for `identifier`), built concurrently. Search without `index` is unchanged.
  The client gains `search(index=, fuzzy=, min_score=)`, `register_tenant(search_indexes=)` and
  `SearchHit`.

## 0.2.1 — 2026-10-06

graphos 0.2.1 makes several API replicas behind one database work correctly.

### Upgrading from 0.2.0

Nothing to do: no schema change. Two optional settings are new, `GRAPHOS_REGISTRY_SYNC_S` and
`GRAPHOS_CACHE_MAX_AGE_S` (both 30 seconds). Each replica now holds one `LISTEN` connection, as in
0.2.0 (it is shared with the change feed).

### Fixed

- **Several API replicas.** In 0.2.0 a tenant registered, extended or unregistered through one
  replica stayed invisible to the others until they restarted (a new tenant answered `404`, new
  labels `400`), the resolver catalogue was never refreshed there, and a K/V cache value could be
  served stale indefinitely after another replica changed or deleted it. Now changes are announced
  with Postgres `NOTIFY` and applied by every replica at once; a replica that misses one looks an
  unknown tenant up before answering `404` and re-reads every registration every
  `GRAPHOS_REGISTRY_SYNC_S` seconds, and serves a cache entry from memory for at most
  `GRAPHOS_CACHE_MAX_AGE_S` seconds. The rate limit and the tokens file stay per replica.

## 0.2.0 — 2026-10-06

graphos 0.2.0 lets clients edit single nodes and edges safely (JSON Merge Patch with
revisions and `If-Match`), adds read-only tokens, shortest paths and a change feed that
publishes every write, and runs long operations as background jobs. Every endpoint is now
plain SQL, which makes large tenants much faster and keeps every query under the server's
timeout.

### Upgrading from 0.1.0

No manual migration. On its first start, 0.2.0 brings existing databases up to date by
itself:

- adds the revision column `graphos_rev` to every label table (metadata only, no table is
  rewritten: under 0.1 s for 149 label tables with 8M edges);
- builds a btree on each edge table's graphid with `CREATE INDEX CONCURRENTLY`, so writes
  are not blocked (12 s for an 8M-row table);
- analyzes label tables that have never been analyzed;
- creates `graphos_meta.jobs`, `graphos_meta.changes` and `graphos_meta.change_horizon`.

Run every replica on the same version: 0.1.0 does not bump revisions or record changes, so
its writes would be missing from the change feed.

Set `"access": "read"` or `"write"` on each entry of the tokens file. Entries without it keep
working as write tokens and graphos logs a warning naming their consumers.

### Changes that can affect existing callers

- **Ids in several labels.** `GET …/graph/nodes/{id}` and `POST …/graph/nodes/{id}/properties`
  answer `409` with the candidate labels when the id exists in more than one label (0.1.0
  returned an arbitrary one, and the merge updated all of them). Pass `?label=`.
  `DELETE …/graph/nodes/{id}` still deletes the id in every label unless `label` is given.
- **Read-only tokens.** Every operation now declares `read` or `write` (shown as the bearer
  scope in `/openapi.json`); a read-only token gets `403` on writes, admin or not.
- **Audit log.** It records the operations declared as writes, whatever their HTTP method:
  reads sent as `POST` (`nodes:scan`, aggregates, `community-ids`, …) are no longer audited,
  and writes refused with `401` / `403` now are.
- **`edges/aggregate`.** A database error fails the request (`400`, timeout `504`). 0.1.0
  silently left that edge type out of the answer, which on a large type meant an empty
  result. Ties are ordered by source value, destination value, then edge type.
- **`nodes/aggregate`.** Ties in count are ordered by value.
- **Louvain.** The same graph now always gives the same communities, whatever order its rows
  are read in; the first recompute after the upgrade can therefore differ from 0.1.0's.
- **Generated columns** may no longer be named `id`, `properties`, `start_id`, `end_id`,
  `end_community_id` or `graphos_…` (`422` at registration).
- **Responses gain fields.** Node and edge reads, patches and the property merge return
  `rev` and `etag` (also the `ETag` header).
- **Shutdown.** Requests still open `GRAPHOS_SHUTDOWN_GRACE_S` (5) seconds into a stop are
  cancelled; change-feed streams, which never end by themselves, are closed and their clients
  resume.
- **JIT** is off for graphos's connections (`GRAPHOS_DB_JIT=true` turns it back on).
- **Client:** node ids and edge types in URL paths are now percent-encoded, so ids
  containing `/` work; callers that encoded ids themselves must stop doing so.

### Added

- **Single nodes and edges.** `GET | PATCH | DELETE …/graph/edges/{type}?src=&dst=` and
  `PATCH …/graph/nodes/{id}`. Patches are JSON Merge Patch (RFC 7396): `null` removes a key,
  nested objects merge. The row is read, locked and written back in one transaction.
- **Revisions and `If-Match`.** Every node and edge has a revision bumped by every write. A
  single node or edge is returned with `ETag: "<graphid>.<rev>"`; `If-Match` on its patch,
  delete or property merge writes only if it has not changed since (`412` with the current
  ETag otherwise). `GRAPHOS_REQUIRE_IF_MATCH=true` makes `If-Match` mandatory there (`428`).
- **Read and write tokens.** `"access": "read" | "write"` in the tokens file.
- **Shortest paths.** `POST …/graph/paths`: every shortest path between two nodes (≤ 8 hops,
  ≤ 100 paths), searching from both ends with one SQL statement per level; a level touching
  more than 100,000 edges stops the search with `truncated`.
- **Change feed.** Every write is recorded in its own transaction. `GET …/changes?after=`
  pages through the records after a cursor, `GET …/changes/head` is where to start after
  reading the data afresh, `GET …/changes/stream` follows them live as Server-Sent Events,
  resumable with `Last-Event-ID`. No committed change is ever skipped, even when
  transactions commit out of order; records are kept for `GRAPHOS_CHANGES_RETENTION_H`
  (168) hours, and an older cursor gets `410`.
- **Background jobs.** `POST …/recompute-communities`, `POST …/graph/edges:resolve` and
  `POST /v1/db/vacuum` accept `Prefer: respond-async` and answer `202` with a job:
  `GET /v1/jobs/{id}`, `GET /v1/jobs`, `POST /v1/jobs/{id}:cancel`. A job runs once across
  replicas, and cancel stops the work itself.
- **Settings:** `GRAPHOS_JOB_CONCURRENCY`, `GRAPHOS_JOB_POLL_S`, `GRAPHOS_JOB_HEARTBEAT_S`,
  `GRAPHOS_JOB_RETENTION_H`, `GRAPHOS_VERTEX_GRAPHID_INDEXES`, `GRAPHOS_DB_JIT`,
  `GRAPHOS_REQUIRE_IF_MATCH`, `GRAPHOS_CHANGES_RETENTION_H`, `GRAPHOS_CHANGES_POLL_S`,
  `GRAPHOS_CHANGES_HEARTBEAT_S`, `GRAPHOS_SHUTDOWN_GRACE_S` (see the README).
- **Client:** `get_edge`, `patch_edge`, `delete_edge`, `patch_node`, `find_paths`;
  `label=` and `if_match=` options; `etag` / `rev` on responses; `submit_recompute_communities`,
  `submit_resolve_edges`, `submit_vacuum`, `get_job`, `list_jobs`, `cancel_job`,
  `wait_for_job`; `changes`, `changes_head`, `iter_changes`, and `stream_changes`, which
  reconnects with `Last-Event-ID`.

### Performance

Measured on a 10.6M-row database whose largest edge type holds 8M rows (0.1.0 → 0.2.0):

| Operation | 0.1.0 | 0.2.0 |
|---|---|---|
| One `edges:scan` page of 50 on the 8M-row type | 181 s, 8.3 GB of temp files | ~1 s |
| Delete one node with 1,175 edges | > 10 min | 27 ms |
| `edges/aggregate`, the 8M-row type | 71 s and an empty answer | 25 s, correct |
| `edges/aggregate`, every edge type | > 4 min | 13.5 s |
| `nodes/aggregate` | 22 s | 1.2 s |
| `export` of 2,000 nodes | `504` after 60 s | 0.3 s |
| Member ids of the 20 largest communities | 69 s | 0.12 s |
| Slowest `/healthz` while Louvain runs | 3.4–4.5 s | ≤ 163 ms |

No endpoint runs Cypher any more: AGE's Cypher executor was seen ignoring both
`statement_timeout` and a cancel for over ten minutes, while every SQL statement honours
both. Louvain runs in a child process, so the API keeps answering during a recompute.

### Fixed

- The Louvain single-flight lock used a 32-bit hash of the tenant name, so two tenants could
  block each other's recompute; it is now a 64-bit key, separate from tenant setup's lock.
- Ids containing a quote or backslash were not found by id; they now match exactly.
- The node property merge updated every label holding the id (now one node, or `409`).

## 0.1.0 — 2026-10-02

First public release: multi-tenant graph service on PostgreSQL + Apache AGE with typed REST
endpoints, COPY-staged bulk writes, composable reads, aggregates, rule-based edges, optional
Louvain communities, a K/V cache, and the async Python client.

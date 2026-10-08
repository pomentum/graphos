# Changelog

Server (`graphos-api`, image `ghcr.io/pomentum/graphos-api`), web UI (image
`ghcr.io/pomentum/graphos-ui`, from 0.3.0) and client (`graphos-client`) are released together
under one version.

## Unreleased

### Added

- **Graph metrics.** `POST /v1/{tenant}/graph/metrics:compute` (admin; `Prefer: respond-async`
  for a job) computes degree, PageRank, weakly connected components and betweenness with
  rustworkx (Apache-2.0, a new default dependency, with numpy) in a child process, and stores them
  on the nodes as `graphos_degree`, `graphos_in_degree`, `graphos_out_degree`,
  `graphos_pagerank`, `graphos_component` and `graphos_betweenness`. Only changed values are
  written (scores rounded to 6 significant digits), so a re-run on an unchanged graph writes
  nothing; component ids carry over between runs; nodes that no longer take part lose their
  values. Betweenness is exact and skipped above `GRAPHOS_BETWEENNESS_MAX_NODES` (20 000). The top
  nodes by a metric come from `nodes:scan` with `order_by`. `/v1/info` reports `metrics: true`.
- Python client: `compute_metrics(…)`, `submit_compute_metrics(…)`, `ComputeMetricsResponse`.
- **Activity in the UI.** What runs on the tenant now (background jobs and direct calls, who
  started each and for how long, Cancel where the session may), a running indicator in the toolbar
  on every page, and the writes, computations and schema changes of the last days (filter by kind,
  person and label; finished jobs with their outcome; older pages on request) instead of only the
  changes since the page was opened.
- **Direct calls are listed as jobs.** Recompute, graph metrics, `edges:resolve` and vacuum called
  without `Prefer: respond-async` are recorded in `graphos_meta.jobs` with `sync: true` while they
  run: visible in `GET /v1/jobs`, cancellable (the waiting caller gets 409), outcome kept.
  `GET /v1/jobs` takes `status` more than once.
- **Feed history:** `GET /v1/{tenant}/changes/history?before=&op=&limit=` reads the feed newest
  first. Python client: `changes_history(…)`, `ChangesHistory`; `list_jobs(status=[…])`.
- **Schema changes in the feed:** `op: schema`, `kind: tenant`, with what was added, removed or
  switched in `detail`; recorded when a registration changes the schema, a declared pair is
  removed, or a tenant is unregistered. Re-registering an unchanged spec records nothing.

### Changed

- The change feed has a new op `schema` and kind `tenant`. The Python client types `op` and `kind`
  as strings, so older clients read them; a consumer should treat an unknown op as informational.
- **`graphos_*` properties are reserved.** Node and edge writes (single, batch, bulk and
  merge-patch) whose properties use a `graphos_` name are refused with `400`; graphos writes them
  itself. Snapshot import still restores them.
- **One whole-graph job per tenant.** A community recompute and a metrics run exclude each other:
  `409` while one runs, and one queued or running job of either kind per tenant.

### Upgrading from 0.4.2

Nothing to do. Writes that used a `graphos_` property name are now refused. The unique index
`jobs_one_active_recompute` is replaced by `jobs_one_active_graph_job` at startup, and
`graphos_meta.jobs` gains a `sync` column (added at startup). A direct whole-graph call
(recompute or metrics) is now refused with 409 while a job of either kind is queued for the tenant.

## 0.4.2 — 2026-10-08

graphos 0.4.2 fixes the UI image, which would not start without graphos-api; that also kept the
image workflow from publishing the images.

### Upgrading from 0.4.1

Nothing to do: no API or database change.

### Fixed

- **The UI image starts without graphos-api.** nginx resolved the API's host name once, at start,
  and refused to start when it did not resolve yet, so the UI could not start before the API, and
  the image workflow's smoke test (which runs the UI alone) failed, which kept the images from
  being published. The address is now looked up per request through the container's DNS: `/api/`
  answers `502` until graphos-api is reachable, then works without restarting the UI. Forwarding
  is unchanged (`GRAPHOS_API_UPSTREAM`, paths, query strings and encoding).
- The image workflow's UI smoke test prints the container's logs when it fails, and checks that
  the UI starts with no API present.

## 0.4.1 — 2026-10-08

graphos 0.4.1 lets Docker Compose enable Louvain community detection on the published image,
without rebuilding it.

### Upgrading from 0.4.0

Nothing to do: no API or database change.

### Added

- **Louvain without rebuilding.** `docker-compose.louvain.yml`, added to the compose command
  (`docker compose -f docker-compose.graphos.yml -f docker-compose.louvain.yml up -d`), enables
  community detection on an image built without it, such as the published one: graphos-api
  downloads python-igraph into its own container on first start, then starts as usual. No volume,
  no image change; the published images stay free of GPL code.

## 0.4.0 — 2026-10-08

graphos 0.4.0 makes the schema a definition: relationship types declare the label pairs they join,
optionally enforced on writes, and the web UI draws the schema from them as a diagram.

### Added

- **Declared relationship endpoints.** A relationship type can declare the label pairs it joins
  (`edge_endpoints` on `POST /v1/tenants`, e.g. `{"CALLS": [["Function", "Function"]]}`): the
  schema's associations, served by `GET /v1/{tenant}/schema` and drawn by the UI. Pairs merge by
  union, are checked against the tenant's labels and types, and a child label satisfies a pair
  declared for its parent. `DELETE /v1/tenants/{name}/edge_endpoints` removes one.
- **`strict_endpoints`** (per tenant, off by default): edge writes of a declared type outside its
  pairs are refused (`400` on `graph/edges`, an invalid line on `edges:bulk`) and `edges:resolve`
  leaves those label pairs out. Types that declare no pairs stay open; stored edges are untouched.
- Python client: `register_tenant(edge_endpoints=…, strict_endpoints=…)`,
  `remove_edge_endpoint(…)`, and `TenantSchemaInfo.edge_endpoints` / `.strict_endpoints`.
- **Release tooling.** `node tools/release/version.mjs X.Y.Z` sets the one version of server,
  client and UI in all six places it lives, rolls the changelog and dates the whitepaper; `--check`
  runs in CI and in both release workflows (which now also require a changelog entry for the tag).

### Upgrading from 0.3.0

Nothing to do: two tables are added to `graphos_meta` at startup (`edge_endpoints`,
`tenant_settings`). Existing tenants declare no pairs, so every write behaves as before. The
declarations live outside the stored spec on purpose: an older server rewriting a spec during a
rolling upgrade or after a rollback cannot drop them.

### Fixed

- `GET /v1/{tenant}/schema` lists a rule-based edge type once in `rule_edge_labels`, however many
  rules it has.

### Web UI

- **Schema diagram** (Schema → Diagram): the schema drawn as a tidy grid with React Flow, laid out
  by ELK in a web worker. A label others inherit from holds them in a block (nested as
  inheritance nests); the other labels follow, busiest first. Selecting a label draws its
  relationship lines with their types and edge counts and opens a panel (properties, lines in
  and out, Browse, Show on map, New node). The lines are drawn from the declared label pairs
  (solid), with pairs in the data outside them dashed in the warning colour and, on request,
  pairs of types that declare none dotted. Boxes and blocks can be moved (remembered in the
  browser) and put back with Tidy up. Administrators add labels, declare a pair by dragging from
  one label to another or from a line's panel, remove one, and switch enforcement on (after a
  confirmation listing the pairs in the data it would refuse). Loaded only when opened.
- The list view shows each relationship type's declared pairs and how many pairs in the data lie
  outside them.
- Explore's new-edge dialog follows the declared pairs: types the source label may start are listed
  first, the others apart (hidden when the pairs are enforced); an undeclared pair is flagged once
  the target is chosen, and cannot be sent when enforced.
- Diagram captions are placed together so they no longer cover each other or the label boxes.
- New node from the schema diagram opens Explore's new-node dialog with the label chosen.
- The header has one colour across its whole width.
- New UI dependencies: `@xyflow/react` (MIT) and `elkjs` (used under EPL-2.0, unmodified); see
  `THIRD_PARTY_NOTICES.md`.

## 0.3.0 — 2026-10-07

graphos 0.3.0 adds the web UI, ranked search through search indexes, a map of a tenant's labels and of its
communities, and a property catalogue kept current as nodes are written.

### Upgrading from 0.2.1

Five tables are added to `graphos_meta` at startup: the property record (`node_properties`,
`node_properties_complete`) and the community map (`community_map`, `community_links`,
`community_map_runs`), which fills at the next Louvain recompute. On the first start, graphos reads every node of
each existing tenant once, in the background, to record its property names (about as long as one
`GET …/graph/catalogue?refresh=true` used to take); the API serves meanwhile, and an
`edges:resolve` that arrives first waits for its tenant's pass.

### Added

- **Community map.** `GET …/graph/communities/map` returns the communities of the last Louvain
  recompute, largest first, each with its size, members per label, best-connected member and
  internal edge count, and the edges between communities per relationship type. The recompute
  stores it from the edges it already clustered, so reading it is exact and instant; it is
  `available: false` until a recompute has run.
- **Graph map.** `GET …/graph/map` returns every vertex label with its node count and every
  (source label, edge type, target label) with its edge count: a large tenant at a glance.
  `GET …/graph/map/hubs?label=` returns a label's best-connected nodes, a place to start
  exploring. Large edge tables are sampled and their counts flagged `estimated` (about a second on
  a tenant of ten million edges); `exact=true` counts every edge.
- **Search any property without an index.** `GET …/graph/search?property=path` matches the
  substring against that property instead of `name` (the default, which keeps its trigram index).
  Other properties are read row by row, so declare a search index for those searched often.
- **Web UI** (`services/graphos-ui`, image `graphos-ui`; opt in with
  `docker compose ... --profile ui`). nginx serves the app and forwards `/api/` to graphos on the
  same origin; users sign in with their own token (kept for the browser tab only), so read-only
  tokens get a read-only UI and edits are attributed to them. Views:
  - **Explore**, in three modes. *Map*: one bubble per label and a line per pair of linked
    labels, where a tenant of more than 5,000 nodes opens. *Groups*: the Louvain communities,
    named after their best-connected member, with the relationships between them. *Graph*: the
    canvas (a small tenant opens on its whole graph); neighbours are added 20 at a time beside
    the node, the camera follows and the rest fades; Back undoes any step; Collapse, Hide and
    Focus; colour by label or group; inspect, edit, add and delete nodes and relationships.
  - **Path** (shortest paths, ends picked by search on any property), **Overview** (counts and
    value spreads), **Activity** (the change feed, live), **Browse** (a label as a filterable,
    paged table) and **Schema** (labels as a tree, relationship types, search indexes; admins
    add labels, types and search indexes).
  - Edits are saved with `If-Match` and only for the fields changed; a node saved by someone
    else meanwhile is flagged with who saved it. Search opens with `/` (⌘K on Apple devices).
    Settings live in the address, so views can be shared and survive navigation. All text is in
    an i18n JSON file. Light and dark themes; narrow windows and phones supported.
- **Client:** `graph_map()`, `map_hubs()`, `community_map()` and `search(property=)`.
- **`GET …/graph/catalogue?property=…`** narrows the catalogue to the given properties.
- **`GET /v1/me`**: the calling token's consumer, admin flag, access and registered tenants.
- **`GET /v1/{tenant}/schema`**: a tenant's labels, label hierarchy, search indexes and
  rule-derived edge labels, for any token on the tenant (the full spec stays admin-only). The
  client gains `me()` and `schema()`.

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

### Fixed

- **The resolver catalogue is current and cheap.** Which vertex labels carry which properties is
  now recorded as nodes are written (`graphos_meta.node_properties`, same transaction), instead of
  being found by reading every row of the tenant: 80 s on a tenant of 572k vertices, run on every
  `edges:resolve` and every registration. `GET …/graph/catalogue` was cached from registration,
  often before any data, and showed nothing until `?refresh=true`; it is now current after every
  commit. Graphs that predate the record are read once, in the background at startup.
- **`nodes:scan` filters on booleans match.** A `where` value of `true` or `false` (with `=`,
  `!=`, `in`, `not in`) was compared as Python's `True` / `False` and matched no node; values are
  now compared in the JSON text form Postgres reads from the property.
- **Node ids containing `/` are addressable.** `GET`, `PATCH` and `DELETE /nodes/{id}` and
  `POST /nodes/{id}/properties` answered 404 for an id such as a file path (`src/a/B.java`), even
  URL-encoded, because the path is decoded before routing. These routes now take the rest of the
  path as the id.
- **Graph stats count each node once.** `n_vertices` added up the per-label counts, and a parent
  label's count includes its children's nodes, so a tenant with a label hierarchy was overcounted
  (943k reported for 572k nodes). The totals now count the graph's base tables; per-label counts
  are unchanged.

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

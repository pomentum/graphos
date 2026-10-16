# Changelog

Server (`graphos-api`, image `ghcr.io/pomentum/graphos-api`), web UI (image
`ghcr.io/pomentum/graphos-ui`, from 0.3.0) and client (`graphos-client`) are released together
under one version.

## Unreleased

### Added

- **Batch node delete**: `POST …/graph/nodes:delete` deletes up to 1,000 nodes, each by `label`
  and `id`, with their edges, in one transaction. One statement per label, served by the label's
  unique id index (a label takes in its child labels' nodes); one change record per label. A
  node that does not exist is skipped, so a repeat deletes nothing. Two batches that block each
  other on shared edges are run again (three times at most), then answered 503 with
  `Retry-After`, nothing deleted. Returns `nodes_requested`, `nodes_deleted` and `edges_deleted`. Client:
  `delete_nodes()`, with the `NodeRef` and `DeleteNodesResponse` models. The web UI's API layer
  has `deleteNodes()`.
- **CSV import on the server**: `POST …/graph/nodes:csv?label=` and `…/graph/edges:csv?type=&src_label=&dst_label=`
  take the CSV file itself (`text/csv`). graphos reads it (comma, semicolon or tab, detected;
  quoted cells, CRLF, a byte-order mark), finds the id columns, maps every other column to a
  property named after its header and typed after its definition unless `columns` (JSON) says
  otherwise, trims ids, merges rows repeating an id (or two ends) into one record, and writes the
  records through the `:bulk` path: the same checks, 500-row batches, change records and
  revisions. `dry_run=true` checks every row and writes nothing (missing ends looked up). The
  answer counts rows, records, written, refused and skipped, and lists the problems found with
  CSV row numbers. Mapping errors are 422, an unreadable file 400 (a quoted cell never closed
  included), a file over `GRAPHOS_CSV_MAX_BYTES` (256 MiB) 413. Client: `import_csv_nodes()`,
  `import_csv_edges()`, with the `CsvImportResponse`, `CsvIssue` and `CsvColumnPlan` models.
- **Check many edges at once**: `POST …/graph/edges:check` looks up to 1,000 edges, each by type
  and the ids of its ends as `GET …/graph/edges/{type}` names one, from one snapshot, and answers
  each in order: `present` (with its labels, revision, ETag and, with `include_properties`, its
  properties), `absent`, or `ambiguous` (an end id in several labels; the matching label pairs
  are listed). Meant for checking a set of facts against the graph in one call, such as what an
  agent is about to state. Read access. Client: `check_edges()`, with the `EdgeCheckItem`,
  `EdgeCheckResult` and `EdgesCheckResponse` models.
- **What a schema change breaks, in the dry run**: `POST /v1/tenants?dry_run=true` answers
  `breaking` beside `changes` and `warnings`: each change that refuses writes or calls that
  worked, or changes what a read returns (a label or type left out of a replaced schema, a search
  index, label-hierarchy pair or resolve rule removed, `strict_endpoints` turned on or a declared
  pair removed under it, and property rules that refuse more where the mode is `strict`: the
  mode becoming strict, a property made required, a new required property, a type changed, an
  enum narrowed, a pattern, format or item rule changed, a minimum raised, a maximum lowered,
  undeclared keys rejected). Each has a stable `change` code, a sentence and the label's or
  type's row estimate. Additions and relaxations are never listed. The web UI's schema import
  shows them before the warnings.
- **Are the metrics and communities current?** `GET …/graph/stats` answers `analyses`: for graph
  metrics and Louvain communities that have written results, when (`computed_at`), over what
  part of the graph (`scope`), how many nodes and edges were written since
  (`rows_written_since`, from the change feed, null past its retention) and `stale`. A metrics run
  that changes no value counts as a run. Stored in `graphos_meta.analysis_runs`. Client:
  `TenantStatsResponse.analyses`, with the `AnalysisFreshness` model.
- **Search quality held to floors**: a labelled query set (literal identifiers, misspellings with
  `fuzzy`, English sentences with the `text` analyzer) runs through `graph/search` in the
  database tests, which fail when recall@10 or MRR drops below its floor.
- **Web UI: several nodes at once on the canvas.** Ctrl, ⌘ or Shift click adds a node to the
  selection or takes it out; Shift-drag on the empty canvas draws a loop that selects what it
  encloses (with Ctrl or ⌘, added to the selection); Ctrl/⌘+A selects every node. A bar then
  expands them (the first 20, a page of neighbours each), hides them (one step for Back), exports
  them as JSON (with the edges between them) or CSV, or, for writers, deletes them and their
  relationships through `nodes:delete`, after asking (in batches of 1,000; a failure part-way
  leaves the nodes already deleted off the canvas).
- **Web UI: reading and drawing on the canvas.** A minimap of the whole canvas from 40 nodes,
  with the part on screen framed; a click or drag on it moves the camera, and a button hides it.
  The relationship under the pointer shows its type (and its caption). Zoomed far out, the label
  pictograms and the hubs' names are left out. Writers double-click the empty canvas to create a
  node at that spot, and Alt-drag from one node onto another to connect them (only the type is
  asked; declared pairs first, refused ones blocked as in Connect).
- **Web UI: CSV import.** On the Overview, beside the dataset upload, writers import a CSV file
  without writing a loader. The page reads only the file's start, for its header, its separator
  (comma, semicolon or tab, detected) and a preview of the first 20 rows; graphos reads the file
  itself (`nodes:csv` / `edges:csv`). The rows are nodes of a label or relationships of a type
  (rule-derived types left out, declared label pairs offered first); the id columns are picked,
  and every other column is a property, named from its header, of a type (auto, text, number,
  true / false) taken from the label's definitions where they declare one, or left out. With each
  change of the mapping graphos checks the whole file in a dry run: rows and the records they
  merge into, what would be written, refused or skipped, the issues it found (empty, too long and
  repeated ids, ragged rows, cells that do not read as their type, undeclared label pairs), the
  property definitions' verdicts and its first refusals; a mapping it refuses is shown by the
  column grid. The import sends the file as it is, with the upload's progress, Cancel (nothing is
  written before graphos has the whole file) and graphos's answer. The web UI's API layer has
  `csvImport()`.
- **Docs:** development guides, for people and coding agents: the rules for the whole repository
  ([docs/development.md](docs/development.md): docs, pictures and docs tools, test stacks, git,
  privacy) and a `DEVELOPMENT.md` in each package for the
  [service](services/graphos/DEVELOPMENT.md), the [client](clients/python/DEVELOPMENT.md) and the
  [web UI](services/graphos-ui/DEVELOPMENT.md); architecture decision records in
  [docs/adrs/](docs/adrs/README.md), with when one is needed and its template; `AGENTS.md` and
  `CLAUDE.md` at the root point to them.

### Changed

- `graphos_property_violations_total` loses its `tenant` label: `/metrics` needs no sign-in, so it
  no longer names tenants to anyone who reaches the port. It counts by `kind`, `outcome` and
  `rule`; per-tenant problems are in the write responses (`property_warnings`,
  `property_violations`). A dashboard that grouped by tenant must drop that grouping.
- **The OpenAPI document is complete** (`/openapi.json`, `/docs`, `/redoc`):
  - every error a route can answer is declared, with when it happens: 401 / 403 on every route
    that needs a token, 404 for an unregistered tenant on every tenant route, 429 (the rate
    limit), 504 (a statement timeout), and each route's own (400, 409, 413, …);
  - every error has a body schema (`ErrorResponse`, `RateLimited`, RFC 7807 `Problem`), and a 422
    is described as either FastAPI's validation error or the route's own refusal;
  - the routes that take a file or a stream declare it: `application/x-ndjson` for `:bulk` and
    `snapshot-import` (gzip accepted), `text/csv` for `:csv`;
  - every schema and every field has a description.
  Two tests keep it so: a route raising a code it does not declare, or a model or field without a
  description, fails the build.

### Fixed

- `POST …/graph/induced_edges` answered 500 when none of the ids matched a node (the answer left
  out `node_count`). It also missed ids holding a `"` or a `\`, reported them with their JSON
  escapes, and, for an id in two labels, counted the edges of only one of its nodes.
- `POST …/graph/edges:scan`: when the 10 MiB page cap was reached on the first row of the next edge
  type, the page came back without a `next_cursor`, as if the scan were over; the rest of that type
  and the types after it were never returned. The cursor now points to that row.
- `GET …/graph/export` declares `multi: true`: two nodes can be joined by edges of several types,
  which graphology refuses to load into a graph declared `multi: false`.
- `POST …/graph/nodes/{id}/properties` answers 400 for a database error, as the other node writes
  do, instead of an unhandled 500.

## 0.11.1 — 2026-10-16

A fix for databases moved from PostgreSQL 16 / AGE 1.6: their label tables now get the
cache-invalidation trigger AGE 1.8 gives its own, from the compose's import, from graphos-api at
start, and from the manual steps of the upgrade guide. The integration-test script no longer
shares anything with a graphos stack running on the same machine.

### Fixed

- **AGE 1.8's cache-invalidation trigger on databases moved from PostgreSQL 16 / AGE 1.6.** AGE
  1.8 puts a statement-level trigger, `_age_cache_invalidate`, on every label table, so writes in
  plain SQL invalidate the in-memory graph its variable-length patterns read; AGE's upgrade script
  adds it to existing tables, but a dump restored into AGE 1.8 runs no upgrade script, so the
  tables of a moved database came without it. The compose's import (`graphos-db-import`) now adds
  it where missing, as AGE's script does, and graphos-api adds it at start to the label tables of
  every registered tenant (one catalogue read when nothing is missing; nothing before AGE 1.8). A
  database moved by hand runs the same block ([Upgrading the
  database](docs/upgrade-postgresql-18.md#age-18s-cache-trigger)). Nothing graphos does changed:
  it never reads AGE's in-memory graph; anything else that does now sees graphos's writes.
- **`scripts/run-db-tests.sh` runs as a compose project of its own**, `graphos-test`. It started
  the compose file as the `graphos` project, with its container names, volumes, networks and port:
  on a machine running graphos it replaced that stack's database, started the move of its
  PostgreSQL 16 data, and `--down-volumes` deleted `graphos_data` with the test volumes. Its
  containers, volumes, networks, API port (`127.0.0.1:18089`, `GRAPHOS_TEST_API_PORT`) and image
  tag (`graphos-api:test`) are now its own, and it refuses to remove any volume not named
  `graphos_test_*`. It also finds a working Python on Windows, where `python3` can be the
  Microsoft Store stub.

### Changed

- **Docs:** the upgrade guide, the installation guide, the concept, features and AGE notes say
  what the import does with the trigger, how to check it after a move, and that a hand-made
  restore of a 1.6 dump needs it; the README describes the isolated integration-test stack.

## 0.11.0 — 2026-10-15

The label histogram, deprecated in 0.10.0, is removed: `nodes/aggregate` now counts the distinct
values too, and the web UI uses it. The Python client catches up with the server: property
definitions and checks, and the parameters and fields it was missing. The documentation is brought
in line with the code throughout.

### Removed

- **`GET /v1/{tenant}/graph/labels/{label}/histogram`**, the client's `label_histogram` and the
  `HistogramResponse` / `HistogramRow` models. Use `POST /v1/{tenant}/graph/nodes/aggregate` with
  `missing: true` (client: `aggregate_nodes(label=..., group_by=..., missing=True)`): the same
  counts, nodes without the property under `""`, `total` for `n_total` and `distinct` for
  `n_distinct`.

### Added

- **`distinct` on `nodes/aggregate`**: the number of groups (distinct values), `limit` aside, exact
  even when the groups are cut (the histogram reported `-1` then). Client: `distinct` is `None`
  from older servers.
- **Client: property definitions.** `property_definitions()` (`GET …/graph/properties`),
  `check_properties()` and `submit_check_properties()` (`POST …/graph/properties:check`, as a call
  or a background job), with the `PropertyDefinitionsResponse`, `PropertyPolicy`,
  `PropertiesCheckResponse` and `LabelCheck` models.
- **Client: what the server already returned or accepted.**
  - `property_warnings` on the upsert responses, and `property_violations` and
    `property_warnings` on the bulk ones.
  - `preferences` on `me()`.
  - `limit` on `list_communities` (the server's default, 50, applied before).
  - `mode` and `properties` (`"report"` to restore data older than its definitions) on
    `snapshot_import`, and `properties` on `import_dataset`.
  - `properties` and `refresh` on `catalogue`.
- **A test that replacing a run leaves no stale derived edge**: deleting the run's nodes, loading
  the run and resolving again gives exactly what a fresh load gives
  (`tests/test_run_replace_db.py`).

### Changed

- **Web UI:** the Overview's value counts and the Components view read `nodes/aggregate`. The
  Overview's count of distinct values is exact when the list is cut, where it showed `-1`.
- **Web UI exports.** Every Explore view saves what it shows: the map, the groups and the metric
  views (PageRank, betweenness, degree, components) as well as the graph, as PNG, SVG or JSON (the
  map and the groups as the data they drew). An SVG keeps each node's label icon, tinted and sized
  as on the canvas, and embeds the labels' font (Inter, only the subsets its letters need), so it
  looks the same in other tools and on machines without the font.
- **`POST /v1/tenants` shows `maintain` as its scope in the API documentation**, not `write`; it
  also accepts `create_tenants` on a pattern the name matches, as before.
- **Docs**, checked against the code: the configuration table lists every setting; status codes,
  permissions, the Cypher console's refusals, the move to PostgreSQL 18 and the client's coverage
  are brought up to date. A logical restore must be serial: `pg_restore -j` fails on AGE's catalog
  tables ([Upgrading the database](docs/upgrade-postgresql-18.md)).

## 0.10.0 — 2026-10-15

The bundled database moves to PostgreSQL 18 with Apache AGE 1.8, and `docker compose up` moves an
existing PostgreSQL 16 stack's data there by itself; PostgreSQL 16 with AGE 1.6 is deprecated.
Three performance fixes found on PostgreSQL 18, a stable order for capped reads, and a way to take
a tenant into plain AGE. Includes everything in 0.9.1 (the console write guard, the export fix). graphos itself is one image, as before, and
runs on either database: CI runs the server's tests against both.

### Added

- **`tools/export/ndjson_to_age.py`**: loads a dataset export into a plain Apache AGE graph,
  without graphos: one label table per label and type, with AGE's own columns only, so AGE's
  variable-length patterns work on it. For leaving graphos, or for analysis in plain AGE. See
  [Reading graphos's data without graphos](docs/without-graphos.md).
- **Docs:** direct SQL readers (a read-only role per tenant, AGE 1.8 required; installation §8);
  an application that forwards its users' tokens, so the audit names the person (sso §4a);
  measurements on PostgreSQL 18 / AGE 1.8 (apache-age.md §12).

### Changed

- **Capped `neighbors` and `expand` answer the same rows however the table is stored.** When the
  cap cuts a result, the rows returned are the first in graph-id order (the order nodes were
  loaded in), not the first the table happens to hold: a dump and a restore, the move to
  PostgreSQL 18, or rows moved by updates give the same answer (measured: identical on PostgreSQL
  16 and its PostgreSQL 18 restore, where all four capped reads differed before). Below the cap
  nothing changes.
- **PostgreSQL 18 with AGE 1.8.** `graphos-db` runs `apache/age:release_PG18_1.8.0`, pinned by
  digest, on a new volume, `graphos_data_pg18` (the data where it is mounted, `PGDATA` set as
  before PostgreSQL 18's images moved it). PostgreSQL 16 with AGE 1.6 is still supported.
- **The move from PostgreSQL 16, by itself.** Two one-shot services, `graphos-db-export`
  (PostgreSQL 16, no network) and `graphos-db-import` (PostgreSQL 18), dump the database on
  `graphos_data` and load it into `graphos-db` on the first start after the upgrade; `graphos-api`
  waits for them. Everything moves: tenants and their graphs, users and grants, saved queries, the
  change feed, revisions and sessions. Every later start, and a new install, has nothing to move.
  A failed import leaves nothing behind and runs again on the next `up`; the dump is never loaded
  over a database graphos has used. `graphos_data` is kept unchanged, as the way back. The import
  gathers planner statistics in parallel, so `graphos-api` does not analyze table by table on its
  first start.
- **CI against both databases.** The integration tests run against the compose's PostgreSQL 18 /
  AGE 1.8 and against PostgreSQL 16 / AGE 1.6; `scripts/run-db-tests.sh --db-image` picks
  another database image, on a volume of its own.
- **asyncpg 0.31 or newer** (the first tested against PostgreSQL 18).
- **`POST /v1/{tenant}/graph/nodes/aggregate`** takes `missing` (count the nodes without the
  property, under `""`) and returns `total` (every node counted, `limit` aside). Client:
  `aggregate_nodes(..., missing=True)`; `total` is `None` from older servers.

### Deprecated

- **PostgreSQL 16 with AGE 1.6.** It still runs, and CI still tests it, but support will be removed
  in a later release: AGE has no release after 1.6 for PostgreSQL 16, and 1.6 carries out some
  Cypher writes in a read-only transaction and can wedge a database process on a variable-length
  pattern. graphos logs `database deprecated` at start, and `GET /v1/info` reports
  `database_deprecated` (the reason, or `null`). Move with `docker compose up`: see
  [Upgrading the database](docs/upgrade-postgresql-18.md).
- **`GET /v1/{tenant}/graph/labels/{label}/histogram`**: `nodes/aggregate` with `missing: true`
  gives the same counts (`total` for `n_total`). The client's `label_histogram` warns. Both stay
  until a later release.

### Fixed

- **Reads slowed down after their fifth run on PostgreSQL 18.** asyncpg prepares each statement on
  a connection, and after five runs PostgreSQL may keep a generic plan, made without the values.
  For a `nodes:scan` on two property filters PostgreSQL 18 kept one that walked the id index and
  dropped 34,702 rows to return 100: 40–55 ms became 0.7–1.2 s, on every connection that had run
  it five times (PostgreSQL 16 kept the custom plan). graphos's connections now always plan with
  the values (`plan_cache_mode = force_custom_plan`); the same scan stays at 26–60 ms.
- **`nodes:scan` with selective property filters was 8× slower on PostgreSQL 18.** A filter
  converts each row's properties, which the planner prices as one cheap operator; on a compact
  table (a fresh load, or the move to PostgreSQL 18) it then walked the id index in order and
  filtered: 649 matches of 100,000 rows cost 334 ms, where the property indexes and a sort take
  28 ms. A filtered scan now prices the conversion near its cost; through graphos the scan takes
  about 37 ms instead of 350 ms, and a filter that matches most rows keeps the ordered walk.
- **`nodes/aggregate` returned non-string values as Python writes them** (`True`, `['red', 'blue']`):
  they come as JSON now (`true`, `["red", "blue"]`), as the histogram gave them. Strings and
  numbers are unchanged.

### Upgrading from 0.9.x

- **Compose:** update the files and run `docker compose -f docker-compose.graphos.yml up -d`. The
  first start moves the data (about as long as a dump and a restore); see
  [Upgrading the database](docs/upgrade-postgresql-18.md). Once content, remove
  the old data: `docker volume rm graphos_data graphos_upgrade`.
- **Your own database:** graphos still runs on PostgreSQL 16 with AGE 1.6; to move it to 18, see
  the same section.
- **Clients:** `nodes/aggregate` returns booleans, lists and maps as JSON text (`true`, not
  `True`). Move from the label histogram to `nodes/aggregate` with `missing: true`.
- **From 0.9.0:** the console write guard and the export fix of 0.9.1 come with this release too.

## 0.9.1 — 2026-10-15

A security fix for the Cypher console on AGE 1.6, and a fix for exports of large tenants. No
database migration; nothing else changes.

### Security

- **The Cypher console could write on AGE 1.6.** It ran every query in a read-only transaction and
  relied on that alone, but AGE 1.6 carries out `SET`, `REMOVE` and `DELETE` (`DETACH DELETE` too)
  where PostgreSQL's read-only check does not see them; only `CREATE` and `MERGE` were stopped. Any
  user allowed the console (`read` on a tenant, by default) could change or delete that tenant's
  data through it, past revisions and the change feed (the audit log has the console call). A
  query with a write clause (`CREATE`, `MERGE`, `SET`, `DELETE`, `DETACH`, `REMOVE`) is now refused
  (400) before the database, whichever AGE runs. Affects every release with the console
  (`GRAPHOS_CYPHER=true`) on AGE 1.6; the console is off by default.

### Fixed

- **Dataset export of large tenants took hours.** `GET /v1/{tenant}/graph/snapshot` reads each edge
  type through a cursor joined to a table of every vertex's ends. PostgreSQL plans a cursor for
  its first rows, and with no index on that table it chose to compare every edge with every vertex:
  a 251,000-node, 940,000-edge tenant did not finish in 33 minutes. The table is now indexed; the
  same export takes about 70 s.

### Upgrading from 0.9.0

- No database migration, no setting changes. Console queries with a write clause now answer 400
  (they were meant to; writes go through the API).
- Also note: on AGE 1.6, a database role with only `SELECT` can still change data through Cypher
  `SET`, `REMOVE` and `DELETE`. That is AGE's behaviour, outside graphos: give no direct Cypher
  access to graphos's database on AGE 1.6 (AGE 1.8 refuses these writes).

## 0.9.0 — 2026-10-15

A tenant can now declare the properties its labels and relationship types carry, and hold writes to
them: `off`, `warn` or `strict`, on every write path. It can check its stored data against those
definitions and edit them on the Schema page and in the diagram. The Cypher console is hardened from
an evaluation under load:
- a finished query is no longer held for the whole timeout, nor answered with a 500;
- the console is sized for several users;
- each query's temporary files are bounded;
- it runs Cypher's functions only, whichever AGE runs.

Reads no longer hide a database error behind a smaller result. Reads of a tenant's data can be audited.
The API documentation is served without a CDN. Breaking changes are few and listed under *Upgrading
from 0.8.3*:
- `community_id` is graphos's own;
- console queries are limited to Cypher's functions;
- a read the database refuses is now an error.

There is no database migration.

### Added

- **Property schema.** A tenant can declare the properties each vertex label and edge type carries
  (`property_schema` in the registration): per label, type or `*`, each property's `type`,
  `required` or `recommended`, `enum`, `pattern`, `format` (`date`, `date-time`), `minimum`,
  `maximum` and array `items`, and what to do with undeclared keys. Definitions combine from `*`
  through the label hierarchy; a narrower entry adds rules but cannot change a type. `mode` is
  `off` (the default: stored, served, exported), `warn` (writes counted in `property_warnings`) or
  `strict` (`POST …/nodes` and `…/edges` answer 422 with the `violations`; bulk and snapshot lines
  are counted invalid; `PATCH` and the properties merge are checked on the row they leave).
  `snapshot-import?properties=report` restores without refusing. Merged by entry (`null` removes
  one), served by `GET /v1/{tenant}/schema`, carried in the schema file (version 2 when present)
  and recorded in the change feed. Prometheus: `graphos_property_violations_total`. Client:
  `register_tenant(property_schema=…)`.
- **Property report.** `POST /v1/{tenant}/graph/properties:check` (`maintain`, or as a job) reads
  the stored rows as if every rule applied: per label and type, rows with errors and warnings,
  counts by rule, example ids and the undeclared names the rows carry.
- **Effective definitions.** `GET /v1/{tenant}/graph/properties` (`read`): per label and type, the
  mode, the rule for undeclared properties and each definition as combined down the hierarchy,
  with the entry it comes from.
- **Web UI: property schema.** On the Schema page each label and relationship type (and every
  label, every relationship type) opens to its definitions, with their rules, where each comes
  from and the properties its rows carry undeclared; maintainers declare, narrow and remove them,
  set an entry's description, mode and rule for undeclared properties, add one from *Add →
  Property*, set the tenant's mode (strict asks first) and check the stored rows as a job with its
  report. The diagram's side panel shows the same, compact. A write refused by the property
  schema names its first violations.

### Changed

- **Cypher console: Cypher's functions only.** A query that calls a function which is not one of
  Cypher's (as AGE documents them) is refused (400) before the database, as one with a qualified
  call already was. AGE resolves such a name to another extension's function on 1.6 (it reached
  `pg_trgm`'s `similarity()`) and to any function on the search path from 1.7 (`version()`), so the
  rule holds whichever AGE runs. `shortest_path()` and `all_shortest_paths()` (AGE 1.8) are refused
  with the variable-length patterns: they need AGE's in-memory graph.
- **Cypher console sized for several users.** `GRAPHOS_CYPHER_POOL_SIZE` defaults to 5 (was 2),
  and a query waits up to `GRAPHOS_CYPHER_WAIT_S` (10 s, new; was 1 s) for a free connection
  before 429. With the old defaults five concurrent users had one query in five refused.
- **Cypher console: temporary files bounded.** `GRAPHOS_CYPHER_TEMP_FILE_LIMIT` (new, `1GB`) sets
  PostgreSQL's `temp_file_limit` on the console's connections, so a query with many hops is stopped
  before its spilled sorts fill the disk (seen with five explicit hops from a hub). It needs
  `GRANT SET ON PARAMETER temp_file_limit` for graphos's role: the bundled database's init script
  grants it to `graphos_app`; an existing database needs the grant run once (graphos logs it at
  start when missing, and carries on unbounded).
- **Audit: the graph export, and reads on demand.** `GET /v1/{tenant}/graph/export` is logged as a
  `read`, like the dataset export. `GRAPHOS_AUDIT_READS=data` (new; default `exports`) also logs
  every read of a tenant's data, with its query string. `read` lines carry `query`.
- **API documentation without a CDN.** `/docs` (Swagger UI 5.33.0) and `/redoc` (ReDoc 2.5.4) load
  their scripts, stylesheet and icon from graphos (`/api-docs-assets/`), not from cdn.jsdelivr.net,
  so they work where the browser has no internet access; ReDoc no longer asks Google Fonts, and
  Swagger UI's validator badge, which sent the schema's address to validator.swagger.io, is off.
  `tools/docs/api_docs_assets.py` fetches the pinned releases (checked against their published
  checksums); CI checks the files match the pins. Both licences are in THIRD_PARTY_NOTICES.md.
- **`GET /v1/{tenant}/schema`** serves `display` and `property_schema` as declared: keys an entry
  leaves out are left out (they came back as `null` or as defaults), and empty sections are omitted.

- **`community_id` is reserved.** graphos writes it (the community recompute), so a node or edge
  write that sets or removes it is now refused as `graphos_*` writes are: `400` on single and batch
  writes, an invalid line on the bulk endpoints; a generated column may not be named
  `community_id`. Snapshot and dataset import still restore it.
  A client that wrote its own `community_id` must use another name.

### Fixed

- **Cypher console: a finished query could be held for 41 s, then answer 500.** When a query ended,
  the console stopped its watchdog with a cancel, which sometimes did not stop it: the stalled
  requests had their result and were waiting on the watchdog. (The likely place the cancel is lost
  is the watchdog's check whether the caller has gone, Starlette's `is_disconnected`, which runs
  inside an anyio scope that cancels itself; not reproduced in isolation.) The watchdog then ran on
  to its deadline, cancelled and terminated a backend that was idle, and closed a connection asyncpg
  had already released, which raised: 500 after 41 s for a query that had finished. The watchdog now
  stops on an event the query sets when it ends, whatever becomes of the cancel, and its own
  failures are logged, never the answer. Seen under five concurrent console users (about one query
  in 120); under the same load with asyncio's debug mode, 38 stalls in 120 before, none after.
- **Reads that swallowed database errors.** `neighbors` and `expand` skipped a hop, and
  `sample_subgraph` a relationship type or its endpoints, when the database refused the query, so
  a failure looked like a smaller result. They now answer 500 and log the error; a sampled type
  without a table still has no edges.
- **Web UI: schema files with a property schema.** The import dialogs refused version-2 schema
  files (those carrying a `property_schema`) as "from a newer graphos"; they read version 2 now.

- **Cypher console.** A query whose database process the watchdog had to close answered 500
  instead of 499 (cancelled) or 504: graphos tried to close the connection again after asyncpg had
  handed it back to the pool.

### Upgrading from 0.8.3

- **Clients:**
  - A write that sets or removes `community_id` is now refused (400): it is the community
    recompute's. Store your own grouping under another name.
  - A console query that calls a function which is not Cypher's is refused (400).
  - `neighbors`, `expand` and `sample_subgraph` answer 500 when the database refuses a read, where
    they used to return fewer results: treat it as any server error.
  - `GET /v1/{tenant}/schema` leaves out undeclared keys in `display` and `property_schema` (they
    came back as `null` or as defaults): read a missing key as not set.
  - A schema file that carries a `property_schema` is version 2. 0.9.0 reads versions 1 and 2;
    0.8.x refuses version 2.
- **Operators:**
  - No database migration.
  - The console's pool now opens up to 5 connections per replica (was 2): allow for them in
    `max_connections` (16 for the API pool, 5 for the console, per replica).
  - For the console's temporary-file limit, run once as a superuser on an existing database:
    `GRANT SET ON PARAMETER temp_file_limit TO graphos_app;` (new databases get it from the init
    script). Without it graphos logs a warning at start and the console stays unbounded.
  - The API documentation pages need no internet access any more.
- **Property schemas** are off for every tenant until one declares them: nothing changes until then.

## 0.8.3 — 2026-10-14

People who know Cypher can now ask their own questions: an opt-in, read-only Cypher console with
saved queries, and a Query page in the web UI that colours Cypher and suggests the tenant's labels
and properties. A tenant can declare how its labels are drawn (colour, icon, caption, size), seen
by everyone on it, and the canvas gains circular and layered layouts and saves as PNG, SVG or JSON,
tables as CSV. No breaking change: every API change adds to 0.8.2 (new endpoints, a new optional
`display` in registrations and schemas, new response fields), the console is off unless turned on,
and the only migration creates a table.

### Added

- **Cypher console (opt-in).** `GRAPHOS_CYPHER=true` lets people who know Cypher run read-only
  Cypher on one tenant's graph: `POST /v1/{tenant}/cypher` with `$name` parameters answers a table
  (vertices, edges and paths as graphos nodes and edges) and the graph they make. graphos refuses
  calls of database functions (schema-qualified names), variable-length patterns and
  `vertex_stats()` (AGE cannot run those on graphos's tables, and a failed attempt leaves its
  database process stuck) before the query reaches the database, never reuses a connection after a
  failed query, runs
  it in a read-only transaction on a small pool of its own, caps rows
  (`GRAPHOS_CYPHER_MAX_ROWS`) and time (`GRAPHOS_CYPHER_TIMEOUT_S`; a watchdog terminates a query
  AGE is slow to stop, and a cancelled request stops its query), and allows one query per user.
  `GRAPHOS_CYPHER_LEVEL` sets the level it needs (`read`). Saved queries
  (`/v1/{tenant}/cypher/saved`) keep named, parameterised queries for a tenant's readers.
  Client: `cypher`, `saved_queries`, `save_query`, `run_saved_query`, `delete_saved_query`.
- **Label styles.** A tenant can declare how the web UI draws each vertex label (`color`,
  `caption`, `size_by`, `icon` from a fixed list) and edge type (`color`, `caption`): `display`
  in the registration, merged by entry (`null` removes one), checked against the tenant's labels,
  served by `GET /v1/{tenant}/schema`, carried in the schema file and recorded in the change feed;
  the dry run warns about a colour hard to see on either canvas. Client: `register_tenant(display=…)`.
- **Web UI: Query.** Where the console is on, a Query page (under Graph): a Cypher editor
  (Ctrl/⌘+Enter runs) that colours Cypher (clauses, labels, edge types, properties, parameters,
  functions, strings, comments) and suggests as one types or on Ctrl+Space: the tenant's labels
  after `:` in a node, its edge types in an edge, property names after `variable.` (the
  variable's label's first), parameters after `$`, clauses, functions with a hint, the query's
  variables, and a few whole queries to start from. A field for each `$name` parameter; the
  result as a table (CSV), nodes with a dot in their label's colour and edges by type, with rows,
  time and whether it was cut, "Open on the canvas" for its nodes and edges, Cancel while it runs,
  and the tenant's saved queries beside it (maintainers save and delete them; a saved query runs
  with a form of its parameters).

- **Web UI: label styles and layouts.** The tenant's label styles are drawn everywhere a label is:
  the Explore canvas (declared colours first, the other labels taking the colours left free; a
  child label its parent's colour and icon; the caption property as the node's name; size by a
  property; the icon on the node, drawn with @sigma/node-image 3.0.0, MIT; edge colours, and edge
  captions on the selection's edges), the label map, the legend (with icons), the schema diagram's
  boxes and the Query results. On the Schema page each label and relationship type has a swatch;
  maintainers click it to set the colour (the category colours or any `#RRGGBB`), caption, size and
  icon, or reset them. The Graph canvas has a Layout menu: force (ForceAtlas2, as before), a circle
  label by label, or layers along the edges (ELK, up to 500 nodes).

- **Web UI: exports.** The Explore canvas saves as a picture (PNG, drawn by sigma), a drawing (SVG,
  the nodes, edges and labels as shown, editable in other tools) or a graph (JSON: nodes and edges
  with their properties). Browse saves the page shown, or every node matching the filter (paged
  through graphos and written to the file as it arrives), as CSV with the chosen columns. The
  Overview's label and relationship counts and a property's values save as CSV. CSV files are UTF-8
  with a byte-order mark, and a cell starting with `=`, `+`, `-` or `@` is prefixed with `'` so a
  spreadsheet does not run it as a formula. Everything is made in the browser; nothing new on the
  server.

### Fixed

- **Web UI: several relationship types between the same two nodes.** The canvas held at most one
  edge between two nodes: opening, expanding or ranking a graph where two nodes are joined by edges
  of two types (or both ways) stopped the view ("an edge linking … already exists"). Every edge is
  kept now, and edges sharing their two nodes are drawn curved apart (@sigma/edge-curve 3.1.0,
  MIT), so each can be seen and clicked; the SVG export draws them with the same curves.

### Changed

- **Demo tenant and docs.** `08_demo_ecosystem.py` declares label styles for the demo and, where the
  console is on, saves five Cypher queries; the docs' screenshots are taken again
  (`tools/docs/screenshots.py`, which now keeps the tenant in the address, starts each shot signed
  out, and adds the Query page, label styles and a layered canvas), and the whitepaper, README,
  feature survey and Apache AGE notes describe the console, label styles, layouts and exports, and
  AGE 1.6's limits behind the console's refusals.

## 0.8.2 — 2026-10-14

Tenants can now be created, unregistered, restored and deleted with their data, from the API, the
Python client and the web UI; unregistering keeps everything needed to restore a tenant as it was.
The web UI keeps each user's theme and last tenant at graphos, the schema diagram is laid out
along the relationships, and a `meta` key can be removed without replacing the schema. One behaviour change for clients: registering the name of an unregistered
tenant restores it for its owner and is refused (409) to anyone else (see Changed).

### Added

- **Restore an unregistered tenant.** `POST /v1/tenants/{name}/restore` (owner) registers it again
  with the schema, declared endpoints and settings it had. `GET /v1/tenants?unregistered=true` lists
  the unregistered tenants the caller owns (every one for an admin). Client: `restore_tenant`,
  `list_unregistered_tenants`.
- **Delete a tenant with its data.** `DELETE /v1/tenants/{name}?drop_data=true` (owner) deletes a
  registered or unregistered tenant for good, in one transaction: its graph and data,
  registration, cache, community map, finished jobs, change records (a reader holding a cursor into
  them gets 410) and the grants on its name. Refused (409) while a job is queued or running on it,
  or when its tables stay in use for 5 s. Client: `unregister_tenant(drop_data=True)`.
- **User preferences.** `GET /v1/me` carries `preferences`, and `PATCH /v1/me/preferences` (JSON
  Merge Patch) changes them: `theme` and `last_tenant`, one JSON document per user, only known
  names and values (422 otherwise), deleted with the user. Nothing is stored in the browser.
- **Removing a `meta` key.** A registration that sends a `meta` key as `null` removes it
  (`{"name": "acme", "meta": {"draft": null}}`); before, only replacing the whole schema could.
  `meta` keys added, removed or rewritten are now recorded in the change feed and shown by the dry
  run, by key (the text stays in the schema). Client: `register_tenant(meta={"draft": None})`.
- **Web UI: tenants.** "New tenant…" (empty, or with a schema file) in the tenant menu and on the
  tenant chooser, for admins and users who may create tenants; "Unregistered tenants…" with Restore
  and Delete data; "Delete this tenant" at the foot of the Overview for owners: unregister by
  default, or delete with the data after typing the name. The theme and the last tenant follow the
  user to any browser.

### Changed

- **Unregistering keeps the registration.** `DELETE /v1/tenants/{name}` still keeps the data, and
  now also sets the schema, declared endpoints and settings aside and keeps the grants on the
  name, so the tenant can be restored. Registering the name of an unregistered tenant restores it
  first for its owner or an admin, and answers 409 to anyone else: a name another user may create
  never hands them old data. A graph unregistered before 0.8.2 (no kept registration) is an
  *orphan*: only an admin may register it again or delete it.
- **Web UI: the schema diagram follows the relationships.** Up to 40 labels, boxes are placed in
  layers along their relationships and the lines routed between them, with room for each caption,
  so lines no longer cross boxes or blocks and captions no longer cover each other; a larger
  schema keeps the grid. Captions are drawn above blocks, the view is fitted below the toolbar, and
  a tenant without edges shows the relationship types without counts. Importing a schema into
  another tenant refreshes the tenant list.

## 0.8.1 — 2026-10-13

A web UI release: dataset downloads and uploads show their progress and go on while the user moves
to another page. The server, the API and the client are unchanged (released at this version
because the three are versioned together). No breaking changes.

### Changed

- **Dataset download progress (UI).** Bytes received, rows against the file header's counts,
  percentage, elapsed time and a progress bar; Cancel stops it. Where the browser allows it
  (Chromium's File System Access API) the data is written straight to the picked file; elsewhere it
  is kept in memory and saved at the end.
- **Dataset upload progress (UI).** Bytes sent against the file's size, then "graphos is writing"
  until the import answers, then its summary; Cancel stops it (what was already written stays, as
  with any interrupted import).
- **Transfers outside the page (UI).** A running download or upload goes on, and its progress stays,
  when the user moves to another page; a toolbar indicator shows it everywhere, with Cancel, and the
  app asks before the tab is closed while one runs. Nothing is kept in browser storage.
- The Overview's Dataset section has a Download row and an Upload row, and shows the tenant's size.

## 0.8.0 — 2026-10-12

This release lets a tenant's data be exported as one file and imported back, into an empty tenant
for an exact restore: from the API, the Python client and the web UI. Together with 0.7.0's schema
export, a tenant can now be copied, backed up and restored. Uploads are streamed end to end. No
breaking changes.

### Added

- **Dataset export.** `GET /v1/{tenant}/graph/snapshot` streams the tenant as one NDJSON file
  (`graphos.snapshot`, version 1) that `snapshot-import` reads back: a header line (format,
  counts, the schema), every node (each once, with its own label, label by label), every edge, and
  an end line with what was written. One consistent snapshot (`REPEATABLE READ`); flat memory
  (server-side cursors, the edges' ends resolved through a temporary table built once).
  `labels` / `types` narrow it, `computed=false` leaves out `graphos_*` and `community_id`,
  `gzip=true` compresses it. `read`, and recorded in the audit log. A dev copy of about 570,000
  nodes and 10 million edges exported in 6 to 8 minutes with gzip (276 MB), 13 without (2.8 GB),
  with the exporting process peaking at 144 MB.
- **`snapshot-import` reads the dataset file:** it checks the header (another format or a newer
  version: 422) and reports it, says whether the end line arrived (`complete`), decompresses a
  gzip body, and with `?mode=empty` imports only into a tenant without nodes (409 otherwise). When
  the file's nodes carry `community_id`, the edges' copies of their ends' communities are restored
  from them, so community-scoped walks work without a recompute. New setting
  `GRAPHOS_IMPORT_MAX_BYTES` (default 16 GiB, after decompression; 413 beyond).
- **UI: Dataset** on the Overview page: download (straight into a file where the browser allows it,
  gzip and computed values optional) for readers; upload, adding or restoring into an empty tenant,
  with the file's schema registered first for maintainers.
- Python client: `export_dataset` and `import_dataset` (streamed to and from disk; `schema="apply"`
  registers the file's schema first, `mode="empty"` restores), `read_dataset_header`.

### Changed

- **Uploads are streamed.** The audit middleware hashed a request body by reading it whole before
  the endpoint ran, so `:bulk` and `snapshot-import` bodies were held in memory. It now hashes the
  chunks as the endpoint reads them (the same hash). A read can ask to be audited (`audit_this`);
  the dataset export does, logged as `read`.

## 0.7.0 — 2026-10-12

This release lets a tenant's whole schema be exported as a file and imported (here, or into another
tenant) after a dry run of what it would change, from the API, the Python client and the web UI. It
also fixes `nodes:scan` over a label hierarchy and the streamed writes' answer to a unique-index
clash. No breaking changes.

### Added

- **Schema export and import.** `GET /v1/tenants/{name}/spec` answers a tenant's whole schema as a
  file (`graphos.schema`, version 1): labels, edge labels with their resolve rules, the label
  hierarchy, declared pairs and `strict_endpoints`, search and property indexes, generated columns
  and `meta`. The file is a registration body, so posting it to `POST /v1/tenants` imports it
  (renamed, into another tenant); an `_export` header says what it is, and a newer version is
  refused with 422. Sorted and in a fixed order, so it diffs cleanly in git. `maintain`.
- **Dry run of a registration.** `POST /v1/tenants?dry_run=true` checks the request as a
  registration would and answers what it would change (exactly what the change feed then records)
  and what applying it would cost or fail on: a table rewrite for a generated column on a label
  with nodes, a unique index over values nodes already share, a long search-index build, labels a
  replace leaves out. Nothing is written.
- **UI: Export and Import** on the Schema page (maintainers). Import reads the file in the page,
  imports into this tenant or another one (created when new, with `create_tenants`), merges or
  replaces, and shows the dry run before anything is applied.
- Python client: `export_schema`, `import_schema(doc, name=, replace=, dry_run=)` (sending more
  than 5,000 declared label pairs over several calls), and `force_shrink` / `dry_run` on
  `register_tenant`. `TenantInfo` keeps `label_hierarchy` and `n_resolve_rules`.

### Fixed

- `nodes:bulk`, `edges:bulk` and `snapshot-import` answer a write that would break a unique
  property index with 409, as `POST /v1/{tenant}/graph/nodes` does; they answered 400.
- `nodes:scan` of a parent label in `label_hierarchy` reports each child node under its own label;
  it reported it under the parent's, so a scan of every label returned child nodes a second time
  under the parent, and re-importing it duplicated them in the parent. Rows equal on every
  `order_by` key (a non-unique property, or the same `id` in a parent and a child) are ordered by
  graphid, so paging no longer skips the ones after a page boundary.
- Docs: two passages described a snapshot export that does not exist.

## 0.6.0 — 2026-10-09

A registration no longer carries SQL: indexes and generated columns are declared, and graphos writes
the DDL, as it already did for search indexes and resolve rules. A **breaking change** for clients
that send SQL in `indexes` or `generated_columns` (see *Upgrading from 0.5.1*); clients that never
did keep working unchanged.

### Changed

- **Property indexes are declared** (`property_indexes`), replacing `indexes` (SQL statements):
  `{"label": "Document", "properties": ["kind"]}`, with `"unique": true` to refuse duplicates, or
  `"kind": "trigram"` for substring search on one property. graphos builds each one on the
  expression its `where` filters compile to, so the planner uses it (an index written by hand with
  another spelling of the expression was silently never used), and builds it without blocking
  writes (the SQL ran as a plain `CREATE INDEX`, which does). Up to 4 properties; named
  `<Label>_<properties>_<kind>_idx` unless `name` is given.
- **Generated columns are declared** (`{label, column, property, type}`, `type` one of `text`,
  `numeric`, `boolean`, `jsonb`), replacing `ALTER TABLE` statements. A value of another JSON type
  gives `NULL` rather than failing the write; a registered column cannot change (422).
- Both are vertex-label only (graphos's reads filter vertex properties), may name a label
  registered earlier, and are reported in full by `GET /v1/tenants/{name}` (`property_indexes`,
  `generated_columns`) instead of `n_indexes` / `n_generated_columns`; the registration response
  has `n_property_indexes`. The change feed lists them (`Document(kind)`, `User(email) unique`)
  instead of counting them.
- A write that would break a unique property index gets **409** (it was a 400 naming the
  database error); declaring one over nodes that already share values gets 422.
- The Python client's `register_tenant` and `TenantSchema` take `property_indexes` and
  declarative `generated_columns`; `TenantSchema` no longer substitutes `{tenant}` (nothing in a
  declaration is tenant-specific).

### Removed

- SQL in `indexes` and `generated_columns` in `POST /v1/tenants`: refused with 422 naming the
  replacement. Empty lists, which clients before 0.6.0 send on every registration, are accepted
  and ignored, so those clients register as before. The SQL validator (`contracts/ddl.py`) is
  gone.

### Fixed

- `scripts/bench_bulk_write.py` and `scripts/bench_parallel_rw.py` sign in as `GRAPHOS_USER` /
  `GRAPHOS_PASSWORD` and renew the token; they still read `GRAPHOS_TOKEN`, a static token that
  0.5.0 removed.

### Upgrading from 0.5.1

- **Clients:** nothing to do for a client that sends no SQL (the 0.5 Python client sends empty
  `indexes` and `generated_columns`, which are accepted); upgrading it gives the new parameters. A
  client that sends SQL: replace `indexes` with `property_indexes` and each `generated_columns`
  statement with its declaration. `CREATE INDEX IF NOT EXISTS doc_kind ON "acme"."Document"
  (((properties::text)::jsonb ->> 'kind'))` becomes
  `{"label": "Document", "properties": ["kind"], "name": "doc_kind"}` (keep `name` to reuse the
  existing index rather than build a second one).
- **Stored specs** are converted when the server first starts: a statement that a declaration
  expresses (a btree or `gin_trgm_ops` index on `((properties::text)::jsonb ->> '<prop>')`, unique
  or not, without `WHERE` or `INCLUDE`; a `text` or `jsonb` column of one property) becomes that
  declaration, keeping the index name. Any other statement is left out of the spec and logged
  (`tenant spec statement left out`, with the statement); what it created stays in Postgres and
  keeps working, but graphos no longer re-creates it.

## 0.5.1 — 2026-10-09

This release brings the graph algorithms into the web UI (run them from Overview, look at the graph
through each one, read the results in a node's panel), keeps the UI signed in and on the same page
across a reload, and fixes community-scoped `/expand`, which could leave its communities through
incoming edges. Run a community recompute once after upgrading (see *Upgrading from 0.5.0*).

### Added

- **Docs: using the results in a client** ([graph algorithms](docs/algorithms.md#using-the-results-in-a-client)):
  ranking search hits by PageRank, focusing a walk on a community, one hit per community,
  bridges by betweenness, keeping hubs in check; with
  `clients/python/examples/09_better_answers.py` running each on the demo tenant.
- **UI: graph algorithms in Overview.** A "Graph algorithms" card shows what the last groups
  recompute (Louvain) and the last metrics run found, and lets whoever maintains the tenant run
  them: the metrics (degree, PageRank, components, and betweenness on request) had no trigger in
  the UI before. One runs at a time per tenant.
- **UI: the graph through one algorithm.** Explore's Groups mode switches between Groups,
  PageRank, Betweenness, Degree and Components. A score view draws the tenant's top 25 to 200
  nodes by that score, across every label, with the relationships among them, sized and coloured
  by the score and ranked beside the canvas; Components lists the islands by size and draws one.
- **UI: what a view shows.** An ⓘ beside the Groups mode's views explains each one: what the
  algorithm finds and how to read the picture (size, colour, lines, what a click does).
- **UI: size by a metric.** Explore's graph can size nodes by PageRank, betweenness or degree, as
  well as by their connections on the canvas.
- **UI: computed values apart.** A node's panel shows its group (named after its hub) and its graph
  metrics in plain words, read-only; the raw `graphos_*` and `community_id` rows are no longer
  listed with the properties one edits.
- Docs: running the published images with Louvain, with a tested compose override
  ([installation guide](docs/installation.md#louvain-with-the-published-images)).

### Changed

- **UI: a reload stays signed in.** The web UI keeps the session in one cookie, `graphos_session`:
  HttpOnly (no page script can read it), Secure, SameSite=Strict, rotated at each renewal and cleared
  at sign-out. The access token still lives in page memory, and nothing is kept in local or session
  storage. Sign-in, the single sign-on callback, password change and refresh take
  `session_cookie: true` to use it (the answer then carries no `refresh_token`, and `session: true`);
  refresh and logout read the token from the cookie when the body has none. Programs and the Python
  client are unchanged. A refresh token used again within 10 seconds of its rotation (two tabs
  renewing at once) gets an access token instead of ending the session. New setting
  `GRAPHOS_AUTH_COOKIE_SECURE` (default `true`).
- **UI: the tenant is in the address.** Pages live under their tenant (`/demo/overview`), so a
  reload, a new tab or a shared link opens the same tenant and page instead of asking to choose a
  tenant again; switching tenant keeps the page. An older link without a tenant still asks when
  there are several.
- **UI: metrics over chosen relationship types.** Overview's Metrics row takes the relationship
  types to count (all by default), and the last run says which it used.
- **UI: Compute groups only where Louvain is installed.** The button reads `GET /v1/info`; on a
  server without the extra the Groups view and Overview say why instead of offering a button that
  answered 501.

### Fixed

- **`/expand` scoped to communities stays inside them in every direction.** A hop walking an edge
  backwards (`direction=in`, and the default `both`) checked the community of the node already
  reached, so a scoped walk could step out through incoming edges (on the demo, 15 of 79 nodes
  reached were from other communities; now none). Each edge now also carries its start's
  community (`start_community_id`, filled by the community recompute, with its own partial index),
  and backward hops check it. `start_community_id` is a reserved column name.
- **The community write-back works in chunks.** The communities of each edge's ends are written in
  UPDATEs of 50,000 vertices each, each committed on its own, rather than one statement per edge
  label: a first fill on millions of edges no longer runs past the 300 s statement limit.
- UI: with fewer groups than the default number shown, the Groups view's "largest" selector was
  blank.

### Upgrading from 0.5.0

Run a community recompute once on each tenant that uses `/expand?communities=…`: until then the
new `start_community_id` is empty, and a scoped walk does not follow incoming edges. The first
recompute writes it on every edge, so it takes longer than usual; later ones write only what moved.

## 0.5.0 — 2026-10-08

This release replaces static tokens with users, service accounts and grants: a **breaking change**
for every deployment (see *Upgrading* below).

### Added

- **Users and sign-in.** People and service accounts sign in with a name and password
  (`POST /v1/auth/login`; a service account's password is its secret; only a scrypt hash is kept)
  and get an access token valid 15 minutes (`GRAPHOS_ACCESS_TOKEN_TTL_S`), a JWT graphos signs
  (ES256, key generated at the first start in `graphos_meta.signing_keys`). People also get a
  refresh token that rotates on each use (`/v1/auth/refresh`; one used twice ends the session),
  `/v1/auth/logout`, and `POST /v1/me/password`. Failed sign-ins lock an account for a while and
  are throttled per address. Disabling a user, resetting its secret or changing its password
  stops its tokens at the next request, on every replica.
- **Grants and levels.** Access is a level per tenant or name pattern (`sol_*`, `*`): `read`,
  `write`, `maintain` (publish schema, recompute groups, compute metrics, which needed admin
  before) and `owner` (grant access to the tenant, unregister it). `create_tenants` on a pattern
  lets a service create tenants and own them without being an admin; `admin` allows everything.
  `GET/POST/PATCH/DELETE /v1/users…` (admin), `GET/POST/DELETE /v1/grants` (admin, or a tenant's
  owner for its tenant). graphos refuses to remove its last admin.
- **Bootstrap admin.** The first start creates the admin `graphos` with the password `graphos`
  (or the one in `GRAPHOS_ADMIN_PASSWORD_FILE`); it must be changed at the first sign-in, always,
  and graphos logs a warning at every start until it is. `python -m graphos reset-admin-password`
  recovers a lost one. `GRAPHOS_LOCAL_ADMIN=false` disables the `graphos` sign-in.
- **Single sign-on (OpenID Connect).** Any provider, by discovery (`GRAPHOS_OIDC_ISSUER`,
  `GRAPHOS_OIDC_CLIENT_ID`, one public client with PKCE). The UI tries a silent sign-in on every page
  load, so someone signed in at the provider is let straight in, back on the page they asked for;
  otherwise "Sign in with SSO". graphos keeps the PKCE verifier and nonce, exchanges the code and
  validates the ID token itself (`/v1/auth/config`, `/v1/auth/oidc/start`, `/oidc/callback`); the
  browser stores nothing. Grants name `oidc-sub:`, `oidc-group:`, `oidc-role:` and
  `oidc-client:` subjects; logout ends the provider session too. Programs may send the provider's
  own access tokens (audience `GRAPHOS_OIDC_AUDIENCE`). Python client: `BearerAuth(source)`.
  `httpx` becomes a runtime dependency of the service.
- **UI:** sign-in with user name and password, the forced password change, change password in the
  account menu, and an **Access** page (users and service accounts with their secret shown once,
  grants on this tenant for its owners, every grant for admins; grants to graphos users and, with
  single sign-on, to the provider's groups, roles, people and programs). Controls follow the level:
  schema editing and recompute need `maintain`.
- Python client: `PasswordAuth(user, password)` (signs in, renews at 80% of the token's life, signs
  in again once on a 401), credentials from `GRAPHOS_USER` / `GRAPHOS_PASSWORD` when no `auth` or
  `token` is given; `GraphosPool(auth=…)` signs in once for all its clients; `Me` has `kind`,
  `must_change`, `levels`, `create_tenants`.

- **Graph metrics.** `POST /v1/{tenant}/graph/metrics:compute` (`maintain`; `Prefer: respond-async`
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
- **Docs:** an [installation guide](docs/installation.md) (first sign-in, users, single sign-on with
  every `GRAPHOS_OIDC_*` setting, production settings, upgrading), a page on the
  [graph algorithms](docs/algorithms.md) and what each one is for, and screenshots of the UI in the
  README, the UI README and the whitepaper.
- **Demo tenant:** `clients/python/examples/08_demo_ecosystem.py` loads an invented open-source
  ecosystem (about 450 nodes in six ecosystems) and runs every algorithm on it; the docs'
  screenshots are of it. `tools/docs/screenshots.py` retakes them, and `tools/docs/diagrams.mjs`
  embeds them in the whitepaper.
- **Schema changes in the feed:** `op: schema`, `kind: tenant`, with what was added, removed or
  switched in `detail`; recorded when a registration changes the schema, a declared pair is
  removed, or a tenant is unregistered. Re-registering an unchanged spec records nothing.

### Changed

- **Static tokens are gone.** `GRAPHOS_AUTH_TOKENS_FILE` and the tokens file are no longer read.
  `access` (`read`/`write`) and `tenants` lists give way to grants.
- `GET /v1/me` answers `{consumer, kind, admin, must_change, tenants, levels, create_tenants}`
  (no `access`).
- Recompute, graph metrics, schema re-registration and removing a declared pair need `maintain` on
  the tenant instead of admin; unregistering needs `owner`; `GET /v1/tenants` lists the tenants the
  caller may maintain.
- Rate limiting counts per signed-in user rather than per token (tokens now change every 15
  minutes); `SIGHUP` no longer reloads anything.
- **The UI stores nothing in the browser**: no cookie, no local or session storage. Tokens live in
  page memory, and a reload signs out; the light/dark choice and a schema diagram's arranged layout
  last while the page is open.
- The change feed has a new op `schema` and kind `tenant`. The Python client types `op` and `kind`
  as strings, so older clients read them; a consumer should treat an unknown op as informational.
- **`graphos_*` properties are reserved.** Node and edge writes (single, batch, bulk and
  merge-patch) whose properties use a `graphos_` name are refused with `400`; graphos writes them
  itself. Snapshot import still restores them.
- **One whole-graph job per tenant.** A community recompute and a metrics run exclude each other:
  `409` while one runs, and one queued or running job of either kind per tenant.

### Fixed

- A cancel noticed twice (the cancel request and the heartbeat of a direct call) could interrupt
  the call while it answered, so the caller got a dropped request instead of `409`. A call is now
  cancelled once.

### Upgrading from 0.4.2

**Authentication (breaking).**

1. Remove `GRAPHOS_AUTH_TOKENS_FILE` and the tokens file: graphos no longer reads them, so the
   tokens stop working (the compose file in this repository no longer sets it).
2. Start graphos, sign in as `graphos` / `graphos` (UI or `POST /v1/auth/login`) and change the
   password; or set `GRAPHOS_ADMIN_PASSWORD_FILE` before the first start to begin with another.
3. For each former token: create a service account (`POST /v1/users`, `"kind": "service"`, or
   the UI's Access page) and grant it what the token had: `read` or `write` on its tenants;
   `create_tenants` on a pattern instead of admin for a service that registers its own tenants
   (it then owns them); `admin` only where needed.
4. Point each consumer at its account: with the Python client, set `GRAPHOS_USER` and
   `GRAPHOS_PASSWORD` (or pass `auth=PasswordAuth(…)`) instead of a token.

Deployments running with `GRAPHOS_AUTH_DISABLED=true` are unaffected.

**Other changes.** Writes that used a `graphos_` property name are now refused. The unique index
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

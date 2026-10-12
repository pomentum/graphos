# Changelog

Server (`graphos-api`, image `ghcr.io/pomentum/graphos-api`), web UI (image
`ghcr.io/pomentum/graphos-ui`, from 0.3.0) and client (`graphos-client`) are released together
under one version.

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

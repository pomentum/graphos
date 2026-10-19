# Developing the graphos service

How to work on `graphos-api`, the FastAPI service in `services/graphos/`.
The rules for the whole repository (docs, test stacks, git, privacy) are in
[`development.md`](../../docs/development.md); they apply here too.

## 1. Where to look

Paths are under `services/graphos/src/graphos/`.

| Question | Look in |
|---|---|
| What a route does, what access it needs | `api/*.py` (one router per area) |
| Request and response shapes (the wire contract) | `contracts/` |
| How a node or edge is written | `writes.py` (every write to graph data) |
| The change feed, its ordering and retention | `changes.py` |
| Property names graphos keeps for itself | `reserved.py` |
| The read-only Cypher console and its guards | `console.py` (and `api/cypher.py`) |
| Sign-in, tokens, per-route access, grants | `auth.py`, `identity.py`, `oidc.py`, `api/access.py` |
| Stored secrets sealed with the master key | `vault.py` |
| The MCP endpoint for agents, its tools | `mcp_server.py` (ADR-0002, ADR-0008) |
| graphos signing people in for MCP clients | `oauth.py`, and `api/auth.py` for the web UI's Allow card |
| The schema summary made for models | `api/schema_summary.py` |
| Reconciling edges by difference | `writes.py` (`reconcile_edges`, `reconcile_resolved_edges`), `api/reconcile.py` |
| Audit log, metrics, logging, rate limits | `observability/` (`audit.py`, `metrics.py`, `logging.py`, `rate_limit.py`) |
| Settings and their `GRAPHOS_*` variables | `config.py` |
| Tenant DDL, registry, start-up maintenance | `bootstrap.py`, `lifecycle.py` |
| Finding one node or edge by identity | `lookup.py` |
| Louvain, PageRank and other algorithms | `louvain*.py`, `graph_metrics*.py`, `graph_compute.py` |
| Database roles and first-start SQL | `services/graphos/deploy/db-init/` |
| The move from PostgreSQL 16 to 18 | `services/graphos/deploy/db-upgrade/` |
| Architecture, write paths, limitations | [`concept.md`](../../docs/concept.md) (§3 internal layout, §4 write paths, §8 known limitations) |
| AGE behaviour, bugs and workarounds | [`apache-age.md`](../../docs/apache-age.md), [`graph_age_practices.md`](../../docs/graph_age_practices.md) |

The module docstrings explain the why; read them before changing a module.

## 2. Setup, test, lint

Python 3.11 (CI's version), in a venv at `.venv` in the checkout
([development.md, Python environment](../../docs/development.md#python-environment)).

```bash
pip install -e "services/graphos[dev,louvain]"     # louvain: python-igraph, GPL, optional
ruff check services/graphos/src services/graphos/tests
pytest -m "not db" services/graphos/tests          # unit tests, no database
```

Without the `louvain` extra, `POST /v1/{tenant}/recompute-communities`
answers 501; tests that need it skip.

Database tests run against the compose stack, as a compose project of its
own (see [development.md §5](../../docs/development.md#5-test-harness-hygiene)):

```bash
bash scripts/run-db-tests.sh                                 # all @db tests, PostgreSQL 18 / AGE 1.8
bash scripts/run-db-tests.sh tests/test_search.py::test_x    # one test
bash scripts/run-db-tests.sh --db-image <image>              # another database image
```

CI runs the integration job on pull requests and on `main`, against the
compose's PostgreSQL 18 / AGE 1.8. graphos-api refuses to start on an older
database (ADR-0007); the move from PostgreSQL 16 stays in
`services/graphos/deploy/db-upgrade/`.

## 3. Data-model invariants

These hold everywhere. Tests enforce most of them; do not work around them.
Changing one takes an [ADR](../../docs/adrs/README.md) first.

- **Every write to graph data goes through `writes.py`.** Nothing else
  inserts, updates or deletes rows of the AGE label tables
  (`tests/test_write_path.py` scans the source and fails otherwise).
- **Every write logs a change record in the same transaction**, with
  `changes.record(...)`. A write that rolls back leaves no record
  (`tests/test_changes.py` checks every write function records).
- **Every row write bumps `graphos_rev`.** Inserts start at 1, updates add 1,
  even when the values do not change.
- **One edge per type per ordered node pair.** The `<E>_endpoints_unique`
  index enforces it and is the `ON CONFLICT` arbiter of every edge write;
  repeated writes merge properties (concept.md §2.3, §8).
- **Reserved property names are refused on writes.** `graphos_*` and
  `community_id` belong to graphos (metrics, communities). Every write
  endpoint calls `reserved.check(...)`; only graphos's own write-backs and
  snapshot or dataset import skip it.
- **All graph writes are SQL, never Cypher.** Label tables carry extra
  columns (`graphos_rev`, the community columns, generated columns). A
  Cypher `SET` / `MERGE` on such a table segfaults the backend on AGE 1.6
  and is refused on AGE 1.8. Reads are SQL too, so `statement_timeout`
  and cancellation apply. Test seeds use SQL writes, not Cypher.
- **Nothing reads AGE's in-memory graph.** No variable-length patterns
  (`-[*1..3]->`), no `vertex_stats()`: AGE refuses to load label tables with
  extra columns and can leave a lock held forever (`console.py`,
  apache-age.md §5).
- **The Cypher console is read-only and guarded.** It is on unless
  `GRAPHOS_CYPHER=false` (ADR-0006). Write clauses, schema-qualified calls and
  non-Cypher functions are refused before the database; the query runs in
  `BEGIN READ ONLY` on its own pool, bounded and cancellable. It is the only
  place that sends Cypher to the database
  (`tests/test_console.py::test_only_the_console_sends_cypher_to_the_database`).
- **Every route declares its access.** A route takes one of
  `ReadPrincipal`, `WritePrincipal`, `MaintainPrincipal`, `OwnerPrincipal`
  or `AdminPrincipal` (`auth.py`); on a `{tenant}` route that holds the
  caller to the level on that tenant. Other routes call `require_level`
  for the tenant they act on. `tests/test_access.py` fails on an operation
  that declares none. The two surfaces outside the API's routers check
  their callers themselves: `/mcp` takes a token like any route and runs
  each tool as a route, as the caller; `/oauth/` is the sign-in itself.
- **A token an MCP client got through graphos only reads.** Its principal
  is held to `read` where it is made (`auth.reading_only`), and the account
  changes a route declares as reads are refused by name
  (`auth.READ_ONLY_REFUSED`; `tests/test_read_only.py`).
- **A rule's edge and a consumer's edge stay apart** (ADR-0001). Edges a
  rule derives carry `graphos_derived`; a consumer's write to one clears
  it; `edges:reconcile` removes only unmarked edges and `edges:resolve` in
  reconcile mode only marked ones.
- **Names are checked, values are bound.** Tenant, label and edge-type
  names are interpolated into SQL only after they are checked against the
  tenant's registered spec; values always go in as bind parameters.
- **State-changing requests are audited** (`observability/audit.py`); a
  large read (export, console query) asks to be audited with `audit_this`.

## 4. Code conventions

- **Ruff** with the config in `pyproject.toml`: line length 100, target
  `py311`, rules `E F I B UP SIM RET PL`. Ruff is held to 0.16.x
  (`>=0.16,<0.17`), so a new minor release cannot change the rules
  unannounced; a patch release can still bring new findings. CI installs
  the newest 0.16.x: lint with the same. A `# noqa` names the rule and
  says why.
- **Typing:** `from __future__ import annotations` at the top of every
  module; every function annotated; modern syntax (`str | None`,
  `collections.abc`). The mypy config is `strict`.
- **Docstrings:** every module opens with a docstring that says what it is
  for and why it works the way it does (constraints, AGE behaviour, which
  test enforces it). Functions get a one-line summary, more only when it
  helps. reST markup: ``` ``name`` ```, `:func:`, `:class:`, `:mod:`.
- **Comments** explain a reason the code cannot show. No comments that
  narrate the code line by line, no notebook-style section banners for
  every few lines, no commented-out code.
- **One place per concern:** writes in `writes.py`, wire shapes in
  `contracts/`, one router per area in `api/`. Linear code over clever code.
- **The API contract** is `GET /openapi.json`. Describe every new operation
  and parameter there; a change in `contracts/` is a public-API change.

## 5. Tests

- Tests live in `services/graphos/tests/test_<area>.py`.
- A test that needs the database is decorated with `@requires_db` (from
  `conftest.py`): it marks `db` and skips without `GRAPHOS_TEST_DSN`. Files
  whose tests all need it are named `test_<area>_db.py`.
- Test names are sentences: `test_write_clauses_are_found_and_nothing_else`.
- Seed test data with SQL writes (through `writes.py`), not Cypher: AGE 1.8
  refuses Cypher writes on graphos's label tables.
- Fixtures use neutral names (`acme`, `Doc`, `LINKS_TO`); see
  [development.md §7](../../docs/development.md#7-privacy).

## 6. When the service changes

Besides the shared list in [development.md §3](../../docs/development.md#3-docs-discipline):

- an API change updates the client and its models
  ([clients/python/DEVELOPMENT.md](../../clients/python/DEVELOPMENT.md)) and, when the UI uses
  it, `services/graphos-ui/src/api/` ([services/graphos-ui/DEVELOPMENT.md](../graphos-ui/DEVELOPMENT.md));
- a new setting lives in `config.py`; one users are likely to touch also
  goes into the README's Configuration table;
- AGE behaviour found or re-verified goes into [`apache-age.md`](../../docs/apache-age.md);
- a new endpoint family, a change of supported database or AGE version, or
  a dependency with licence consequences gets an [ADR](../../docs/adrs/README.md) first.

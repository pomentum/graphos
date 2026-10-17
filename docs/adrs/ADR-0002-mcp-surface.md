# ADR-0002: An MCP surface for agents, and a schema summary

- **Status:** Accepted (2026-10-16)
- **Scope:**
  - a new transport, `/mcp`, in the graphos app;
  - a new read endpoint, `GET /v1/{tenant}/schema/summary`;
  - a token endpoint for service accounts.
- **Related:** ADR-0003 (variable-length Cypher), which decides what `run_cypher` may run.

## Context

Agents reach graphs through the Model Context Protocol (MCP). graphos has none, so an agent today
needs custom code against the REST API.

**Prior art does not fit.** The MCP servers graph databases ship today:
- offer a schema tool and a read-Cypher tool, often a write-Cypher tool too, some classifying reads
  with `EXPLAIN`;
- some sign in with OIDC, but rely on protocol sessions, which the current specification dropped;
- one for PostgreSQL graphs runs Cypher in `READ ONLY` transactions;
- **none** has per-tenant grants or an audit that names the person.

graphos has both already:
- grants per tenant, checked on every route (`require_principal`, access levels `read` to `admin`);
- an audit middleware that can log data reads (`GRAPHOS_AUDIT_READS=data`);
- a Cypher console that refuses writes and database functions before the database
  (`console.py`), with timeouts, row caps and a watchdog.

**The protocol** (specification 2026-07-28) and the Python SDK `mcp` 2.3 (MIT):
- **Stateless:** each request stands alone, with no protocol session, so any graphos replica serves
  any call.
- **The SDK:**
  - its Streamable HTTP app mounts inside a FastAPI app;
  - the host's lifespan starts its session manager;
  - its DNS-rebinding protection refuses unknown `Host` values with 421.
- **Authorization:**
  - The MCP server is an OAuth 2.1 resource server.
  - It serves protected-resource metadata (RFC 9728) at the host root.
  - Tokens must name the MCP resource as their audience.
  - **Passing a client's token through to another service is forbidden.**
- **Hints only:** tool annotations (`readOnlyHint` and the rest) are not enforced, so the server
  must enforce.
- **Deprecated:** logging, sampling and roots.

**Agents need the schema first.** A schema summary is what every graph agent gets before writing
queries. graphos can compose it from what it already serves.

## Decision

### 1. The schema summary

`GET /v1/{tenant}/schema/summary`, which takes `Read` access, as `text/plain` (compact, for a
model) or JSON.

**Content:**
- labels with node counts, and their properties: the declared type and enum from the property
  definitions, otherwise the sampled names;
- label pairs per edge type, `(src)-[:TYPE]->(dst)`, with counts;
- search indexes, and saved queries with their typed parameters;
- per label and property, the number of rows that have it and of distinct values; for string
  properties with at most 20 distinct values, those values with their counts. A model that
  filters on a value it guessed from the key's name (`status = "active"` against rows that say
  `ACT`) gets nothing and reports it as a fact;
- caps that say what they hid ("12 of 40 properties shown"), and a last line telling the model to
  match names and values literally and to look values up (`get_property_values`) before filtering.

**What it leaves out:**
- computed `graphos_*` properties and `community_id`, listed once as "computed by graphos";
- the whole property-definitions document (26k characters on the sample).

**Cache:**
- kept per tenant;
- dropped on any `schema` change record, or after `GRAPHOS_SCHEMA_SUMMARY_TTL_S` (default 300);
- the label-pair counts come from `graph/map`, which is the slow part (1.8 s on the sample).

### 2. Mounted in the graphos app, off by default

**The setting:** `GRAPHOS_MCP=true` mounts the SDK's Streamable HTTP app at `/mcp`, in stateless
mode.

**Each tool runs the REST route it wraps, in process, with no network hop:**
- graphos's logic lives in its routes, so a tool calls its route through the app's own ASGI
  interface, nothing reimplemented;
- under a single-use token lent for the caller (256 random bits, valid in this process for that
  one call, never sent anywhere), so the route holds the caller to its grants, timeouts and caps,
  and the audit records the call with the tool's name;
- this is not token passthrough: graphos lends the token to itself, the client's token stays at
  `/mcp`.

**Not a sidecar:**
- the specification forbids a sidecar from passing the client's token through to graphos;
- so it would need credentials of its own, and graphos would see the sidecar, not the person;
- the audit would lose who asked, and the sidecar would be a confused deputy.

### 3. Authorization

**Option A: graphos's OIDC provider is the authorization server.**
- **Metadata:** graphos serves the protected-resource metadata at
  `/.well-known/oauth-protected-resource` and `/.well-known/oauth-protected-resource/mcp`. The
  metadata names `GRAPHOS_OIDC_ISSUER` as the authorization server and `GRAPHOS_MCP_RESOURCE` (for
  example `https://graph.example.com/mcp`) as the resource.
- **Token check:**
  - On `/mcp`, an OIDC access token must carry `aud = GRAPHOS_MCP_RESOURCE`.
  - It is then mapped to a principal as the API maps tokens today (consumer, groups and roles
    claims).
  - A token for the REST API's audience is refused on `/mcp`, and the reverse.
- **A refused call** answers 401 with `WWW-Authenticate: Bearer resource_metadata=…`, so clients
  discover the flow.
- **Provider notes:**
  - Some providers need an audience mapper for the MCP resource.
  - Providers without RFC 8707 resource indicators need a client scope that sets the audience; this
    is documented per provider in `sso.md`.

**Service accounts (no browser):**
- `POST /v1/auth/token` with `grant_type=client_credentials`, for graphos's service accounts (users
  whose password is a secret, `users/{name}:reset-secret`).
- It issues a graphos-signed access token. The requested `resource` (default: the REST API) becomes
  its audience.
- Any service that signs in with a password today can use this endpoint instead.

**Without OIDC:** deployments with local users only use client credentials, and an MCP client
configured with a static `Authorization` header.

**Not now: graphos as a full authorization server** (option B: dynamic client registration and
authorization pages). It is more code, more attack surface, and the specification's confused-deputy
rules apply. It can be revisited if clients cannot use the organisation's provider.

### 4. Tenants

**One endpoint `/mcp`:**
- every tool except `list_tenants` takes a required `tenant` argument;
- `list_tenants` returns only the tenants the caller holds a grant on;
- the grant is checked on every call, so a tenant name the caller has no grant on is refused like a
  missing one.

**Later:** a pinned endpoint `/t/{tenant}/mcp` (no `tenant` argument). It is needed for per-tenant
prompts (below).

### 5. Tools, version 1: reads only

**The tools:**

| Tool | Service function behind it |
|---|---|
| `list_tenants` | the caller's grants |
| `get_schema` | the schema summary (1) |
| `search_nodes` | `graph/search` |
| `get_node` | one node by id (and label when the id is in several) |
| `get_neighbors` | `graph/neighbors` |
| `expand_subgraph` | `graph/expand` |
| `shortest_paths` | `graph/paths` |
| `aggregate_nodes`, `aggregate_edges` | `graph/nodes/aggregate`, `graph/edges/aggregate` |
| `scan_nodes` | `graph/nodes:scan`, cursor-paged |
| `list_communities`, `community_summary` | `graph/communities`, `graph/community-summary` |
| `list_saved_queries`, `run_saved_query` | `cypher/saved`, `cypher/saved/{name}:run`, typed parameters only |
| `get_property_values` | `graph/nodes/aggregate` with `distinct`: the values a property really takes, with counts |
| `get_graph_map`, `get_property_definitions`, `get_graph_stats` | `graph/map`, `graph/properties`, `graph/stats` |
| `recent_changes` | `changes/history`: the latest writes, newest first |
| `get_saved_query` | `cypher/saved/{name}`: one saved query's text and parameters |
| `run_cypher` | `POST /v1/{tenant}/cypher`, through the console's guard; offered only where the console is on |
| `check_edges` | `graph/edges:check`: for up to 1,000 `(src, type, dst)` triples, whether each edge is present, with its revision |

**How they behave:**
- **The tool list is fixed and static:**
  - tool names, descriptions and schemas never contain tenant data (labels, property names,
    descriptions). That rules out tool poisoning through data a tenant writes;
  - tenant specifics reach the model only through `get_schema` and results.
- **`readOnlyHint: true`** on every v1 tool. It is a hint for clients; the server enforces read-only
  by granting only `Read` operations through MCP.
- **One access level per tool definition:** each tool states the access level it needs once
  (`read` for every v1 tool); the hint and the grant check both derive from it, so they cannot
  disagree. A tool without one fails the test suite, as a route without one does
  (`tests/test_access.py`).
- **Results:**
  - structured content, with a text copy;
  - capped by `GRAPHOS_MCP_MAX_RESULT_BYTES` (default 256 KiB);
  - a capped result says so and how to page (`truncated`, the next cursor).

**`run_cypher`** (brought forward from version 1.1 into version 1):
- only where the console is on (`GRAPHOS_CYPHER=true`) and the caller's grant allows it
  (`GRAPHOS_CYPHER_LEVEL`);
- it goes through the same guard, timeout, row cap and watchdog, in `BEGIN READ ONLY`;
- variable-length patterns follow ADR-0003.

**Later:**
- **Query-time analyses** (impact, dominators, personalized PageRank, weighted paths), as tools,
  once their REST endpoints exist.

**Writes are a later ADR:**
- typed tools, never a `write_cypher`;
- each write is a change record with the person's name, as today.

### 6. Resources and prompts

**Resources (v1):**
- `graphos://{tenant}/schema/summary`;
- `graphos://{tenant}/properties` (property definitions);
- both carry a `ttlMs` matching the cache.

**Prompts (later, with the pinned endpoint):**
- per-tenant analysis templates, kept like saved queries: `name`, `description`, `input_fields`,
  `system_prompt`, `user_prompt`;
- a prompt is tenant-written text and is treated as untrusted, as data is.

### 7. Security rules

- **Untrusted data:**
  - data in the graph is untrusted input to the model;
  - results are returned as data (structured content), never merged into tool descriptions or
    instructions;
  - the documentation warns consumers about prompt injection through node text.
- **Audit:**
  - **every MCP call is audited**, reads included, whatever `GRAPHOS_AUDIT_READS` says. Reads are
    the exfiltration path;
  - the audit record names the tool and the tenant.
- **Limits:**
  - the existing per-token rate limiter applies to MCP calls. It is off by default
    (`GRAPHOS_RATE_LIMIT_ENABLED`), and the MCP documentation recommends turning it on;
  - the existing per-route timeouts and caps apply.
- **Host and Origin:**
  - DNS-rebinding protection and `TrustedHostMiddleware` take one list, `GRAPHOS_ALLOWED_HOSTS` (a new setting);
  - `Origin` is checked when present.
- **Errors:** errors carry no SQL, stack traces or other tenants' names, as the REST API.

## Decision options

| Option | For | Against |
|---|---|---|
| **In-app MCP, read tools, OIDC option A plus client credentials (chosen)** | one grant model, one audit that names the person; stateless, so any replica; no new service to run | a new dependency (`mcp`) in the image; graphos must follow protocol changes |
| A sidecar MCP service calling the REST API | graphos unchanged | the specification forbids passing the token through; the sidecar would need its own credentials, and the audit would lose the person |
| Only `run_cypher` (the shape of the graph databases' own servers) | small surface | Cypher-only agents miss graphos's typed reads (expand, paths, aggregates), and the console may be off; typed tools are safer and cheaper to cap |
| graphos as its own authorization server (option B) | works without an organisation's provider | dynamic registration and authorization pages to build and secure; confused-deputy rules |
| Per-tenant endpoints only (`/t/{tenant}/mcp`) | the simplest tenant isolation | one client configuration per tenant; cross-tenant agents cannot list what they may see |

## Evidence

**The schema summary**, composed from today's endpoints on a 940k-edge sample graph:

| Part | Time | Size |
|---|---|---|
| `graph/map` (labels, pairs, counts) | 1,834 ms | 2,151 characters |
| `graph/catalogue` (property → labels) | 29 ms | 3,303 characters |
| `schema` | 147 ms | 9,133 characters |
| Composed summary (8 labels, 16 pairs, 114 label-property entries) | about 1.9 s uncached | **2,353 characters, about 590 tokens** |

**Typed reads:** the typed reads the tools wrap are the existing, measured API routes. No new query
path is introduced by v1.

## Consequences

- **The image:**
  - gains the `mcp` SDK (MIT) and its dependencies;
  - the version is pinned, and protocol upgrades are tested like AGE upgrades.
- **Documentation:**
  - a new page on connecting an agent: the OIDC provider setup per provider, client credentials, a
    static header for local users, and the security warnings;
  - a `features.md` section, and a new configuration section (`GRAPHOS_MCP`,
    `GRAPHOS_MCP_RESOURCE`, `GRAPHOS_MCP_MAX_RESULT_BYTES`, `GRAPHOS_SCHEMA_SUMMARY_TTL_S`).
- **The token endpoint** also serves any non-MCP service account.
- **The audit volume grows** with agent use. Agents call often, so retention guidance belongs in
  the documentation.
- **The schema summary** is useful without MCP: the REST endpoint is shipped first.

## Acceptance

- **Protocol:** an MCP client (the SDK's own client in tests) lists the tools, calls each tool, and
  reads both resources. The calls are on PostgreSQL 16 and 18, with two replicas behind one URL, and
  no session is involved.
- **Tenants and grants:**
  - a principal without a grant on a tenant gets the same refusal as for a missing tenant;
  - `list_tenants` shows only granted tenants.
- **Audience:** a token with the REST audience is refused on `/mcp`, and an MCP-audience token is
  refused on `/v1`.
- **Audit:** every call writes an audit record with the person, the tool and the tenant, with
  `GRAPHOS_AUDIT_READS=exports`.
- **Every tool reachable and classified:** a test lists the tools through the SDK's client and
  checks each one has an access level, `readOnlyHint` matches it, and no v1 tool needs more than
  `read`.
- **Values:** the summary lists the values of a low-cardinality property with counts, says when
  a cap hid properties or values, and `get_property_values` returns the same counts as
  `nodes/aggregate`.
- **Static metadata:** no tool name, description or schema changes when tenant data changes. A test
  diffs `tools/list` before and after writing labels and properties with instruction-like text.
- **Caps:** a result over the byte cap is truncated, with `truncated` and a cursor.
- **Host:** an unknown `Host` is refused with 421 by the MCP app, and with 400 by the API.
- **Schema summary:**
  - under 1,000 tokens on the sample;
  - a cached answer in under 50 ms;
  - dropped on a schema change.

## Open questions

1. Does the console's grant need its own level for `run_cypher` in v1.1, or is `Read` enough while
   the guard stays?
2. The protocol revision to pin at the first release: 2026-07-28, or newer if one lands before.

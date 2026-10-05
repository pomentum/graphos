# graphos usage guide

A practical guide to integrating with graphos, a multi-tenant property-graph service (FastAPI on
PostgreSQL 16 + Apache AGE 1.6). The service code is the reference: routers live in
`services/graphos/src/graphos/api/` and wire contracts in `services/graphos/src/graphos/contracts/`.
The live OpenAPI schema is served at `/docs` and `/openapi.json`.

Examples use tenant `acme`, vertex labels `User`, `Document` and `Tag`, and edge labels `AUTHORED`,
`FOLLOWS`, `TAGGED_WITH` and `WRITTEN_BY`.

---

## 1. Quick start

### 1.1 Secrets, stack, token

```bash
bash scripts/init-secrets.sh                      # or: pwsh scripts/init-secrets.ps1
docker compose -f docker-compose.graphos.yml up -d
curl -s http://localhost:8080/readyz              # {"status":"ready","registered_tenants":0}

export G=http://localhost:8080
export TOKEN=$(cat services/graphos/deploy/secrets/admin_token.txt)
```

`init-secrets.sh` is idempotent (it never overwrites a file). In `services/graphos/deploy/secrets/`
it creates four files: `graphos_pw.txt` (the database superuser's password, used only by the init
scripts), `graphos_app_pw.txt` (the password of `graphos_app`, the role the API connects as),
`graphos_tokens.json` (one random admin token) and `admin_token.txt` (the same token). The compose
file publishes the API on `127.0.0.1:8080` only; put a reverse proxy in front of it for anything
beyond local use, and set `GRAPHOS_FORWARDED_ALLOW_IPS` to the proxy's address (§10).

`graphos_app` is created by `services/graphos/deploy/db-init/01-app-role.sh`. Like every script in
`db-init/`, it runs only when the data volume is empty. A volume created before that script existed
has no `graphos_app` role: create it by hand with the SQL in `01-app-role.sh` (as the superuser), or
start from a fresh volume.

### 1.2 Register a tenant (admin)

```bash
curl -s -X POST "$G/v1/tenants" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "acme",
       "vertex_labels": ["User", "Document", "Tag"],
       "edge_labels": ["AUTHORED", "FOLLOWS", "TAGGED_WITH"]}'
# → {"tenant":"acme",...,"n_resolve_rules":0,"n_resolver_indexes":0,"status":"bootstrapped"}
```

### 1.3 Write, then read

```bash
curl -s -X POST "$G/v1/acme/graph/nodes" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"nodes": [
        {"id": "u1", "label": "User",     "properties": {"name": "Ada", "team": "core"}},
        {"id": "u2", "label": "User",     "properties": {"name": "Lin", "team": "core"}},
        {"id": "d1", "label": "Document", "properties": {"name": "Design notes", "lang": "en"}}]}'

curl -s -X POST "$G/v1/acme/graph/edges" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"edges": [
        {"src_id": "u1", "src_label": "User", "dst_id": "d1", "dst_label": "Document",
         "type": "AUTHORED", "properties": {"at": "2025-11-12T10:00:00Z"}},
        {"src_id": "u2", "src_label": "User", "dst_id": "u1", "dst_label": "User", "type": "FOLLOWS"}]}'
# → {"tenant":"acme","edges_upserted":2,"edges_skipped":0,"by_type":{...},"elapsed_ms":…}

curl -s "$G/v1/acme/graph/nodes/u1" -H "Authorization: Bearer $TOKEN"
# → {"tenant":"acme","external_id":"u1","label":"User","properties":{"id":"u1","name":"Ada",...},"elapsed_ms":…}
curl -s "$G/v1/acme/graph/neighbors?node_id=u1" -H "Authorization: Bearer $TOKEN"
curl -s "$G/v1/acme/graph/search?q=design&label=Document" -H "Authorization: Bearer $TOKEN"
```

### 1.4 Python client

```python
# pip install -e clients/python   (distribution graphos-client, import name graphos_client)
import asyncio
from graphos_client import EdgeUpsert, GraphosClient, NodeUpsert

async def main(token: str) -> None:
    async with GraphosClient(base_url="http://localhost:8080", tenant="acme", token=token) as g:
        await g.register_tenant(vertex_labels=["User", "Document", "Tag"],      # admin token
                                edge_labels=["AUTHORED", "FOLLOWS", "TAGGED_WITH"])
        await g.upsert_nodes([NodeUpsert(id="u1", label="User", properties={"name": "Ada"})])
        await g.upsert_edges([EdgeUpsert(src_id="u1", src_label="User", dst_id="d1",
                                         dst_label="Document", type="AUTHORED")])
        node = await g.get_node("u1")                                         # None on 404
        hits = await g.search("ada", label="User", limit=10)
        print(node, hits.nodes)

asyncio.run(main("<token>"))
```

The constructor takes keyword arguments only: `base_url`, `tenant`, `token=None`, `timeout_s=30.0`,
`retries=3` and `retry_backoff_s=0.5`. Errors raise `GraphosError` (`.status_code`, `.detail`); a
503 raises its subclass `GraphosNotReady`. The client retries network errors and 5xx responses other
than 504, but never a read/write timeout, `edges:resolve` or `recompute-communities`. Without a
wrapper (use HTTP): `snapshot-import`, `catalogue`, `/v1/db/*`, `?force_shrink=true`, and `limit` on
`/communities`.

---

## 2. Authentication and scopes

Auth fails closed. graphos refuses to start without `GRAPHOS_AUTH_TOKENS_FILE` unless
`GRAPHOS_AUTH_DISABLED=true` is set. With auth disabled, every request is an anonymous admin, so use
that setting for local development only.

```json
{
  "tok_admin_…":  {"consumer": "ops",      "tenants": [],       "admin": true},
  "tok_acme_app": {"consumer": "acme-app", "tenants": ["acme"], "admin": false}
}
```

Send the token as `Authorization: Bearer <token>`. There are only two scopes.

| Scope | Grants |
|---|---|
| tenant membership (`tenants`) | read **and** write on `/v1/{tenant}/graph/...` for the listed tenants |
| `admin: true` | every tenant, plus `/v1/tenants*`, `POST /v1/{tenant}/recompute-communities`, `/v1/db/*` |

These endpoints need no token: `/healthz`, `/readyz`, `/metrics`, `/v1/info`, `/` and `/docs`.

**Token rotation.** Edit the tokens file, then run `docker kill --signal=HUP graphos-api`. graphos
swaps in the new token map atomically. SIGHUP is not available on Windows hosts. The file must be
a JSON object. If it is missing, unreadable or not a JSON object at startup, graphos refuses to
start; if that happens on a reload, the error is logged and the tokens already loaded stay in
effect. A malformed entry (for example one without `consumer`) is skipped with an error log, and
the remaining entries still load.

---

## 3. Tenant registration

`POST /v1/tenants` (admin). The body is `TenantSpecBody`:

| Field | Type | Rule |
|---|---|---|
| `name` | str | `^[A-Za-z][A-Za-z0-9_]{0,62}$`. Becomes the AGE graph and Postgres schema name. |
| `vertex_labels` | list[str] | each must match `^[A-Z][A-Za-z0-9_]{0,62}$` |
| `edge_labels` | list[str \| `{name, resolve_rule}`] | same pattern; `resolve_rule` is covered in §9 |
| `indexes`, `generated_columns` | list[str] | validated DDL (see below) |
| `label_hierarchy` | dict child→parent | both labels must be in `vertex_labels`, and a label cannot be its own parent; reads on the parent see the child's rows |
| `meta` | dict[str, str] | free-form |

**Merge vs replace.** By default, re-registering merges the incoming spec into the stored one.
Labels, indexes, generated columns, the hierarchy and rules are unioned, and `meta` keys are
overwritten. A bare `{"name": "acme"}` therefore never shrinks a tenant. `?force_shrink=true`
replaces the stored spec instead. Labels removed this way are **not** dropped from AGE; their tables
and data stay in place. In both modes graphos then serves the stored (merged) spec: the response's
`vertex_labels` / `edge_labels` are the merged sets, and a partial re-registration does not hide
labels registered earlier. A `label_hierarchy` that breaks the rule above returns 422.

**Indexes created automatically.** Per vertex label: `<Label>_properties_gin` (GIN on
`properties`), `<Label>_id_unique` (unique on the `id` property, so ids are unique within a label),
`<Label>_community_id`, and `<Label>_name_trgm` (trigram GIN on `name`, serves `/graph/search`). Per
edge label: unique and plain `(start_id, end_id)` indexes, an `end_id` btree, and a partial index on
`end_community_id`. Any other index has to be declared in `indexes`.

**Allowed DDL** (`contracts/ddl.py`). Each string must be exactly one statement of one of these forms:

```text
CREATE [UNIQUE] INDEX IF NOT EXISTS <name> ON <tenant>.<label> [USING btree|gin|gist|hash|brin|spgist] (...) [INCLUDE (...)] [WHERE ...]
ALTER TABLE <tenant>.<label> ADD [COLUMN] [IF NOT EXISTS] <col> <type> GENERATED ALWAYS AS (...) STORED
```

The schema must be the tenant being registered, and the table one of its own labels (or
`_ag_label_vertex` / `_ag_label_edge`). Unquoted names are lower-cased exactly as Postgres does, so
quote mixed-case tenant and label names (`"Acme"."Doc"`). A generated column's `<type>` must be a
plain type name (`text`, `integer`, `numeric(10,2)`, `double precision`, `jsonb`, `text[]`, …) with
nothing else before `GENERATED`: no `CHECK`, `DEFAULT`, `NOT NULL` or `COLLATE`. Rejected anywhere:
`;` (except one trailing), `--`, `/* */`, `$`, backslashes, prefixed literals (`E'..'`, `U&'..'`,
`B'..'`, `X'..'`) and non-ASCII characters. Violations return 422.

```bash
curl -s -X POST "$G/v1/tenants" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{
  "name": "acme",
  "vertex_labels": ["User", "Document", "Tag"],
  "edge_labels": ["AUTHORED", "FOLLOWS", "TAGGED_WITH"],
  "indexes": [
    "CREATE INDEX IF NOT EXISTS document_lang ON acme.\"Document\" ((((properties::text)::jsonb) ->> 'lang'))"
  ]
}
EOF
```

The expression matches the one `nodes:scan` filters on, so a `where` clause on `lang` can use it.

| Call (admin) | Effect |
|---|---|
| `GET /v1/tenants` | `{"tenants": [TenantInfo…]}` |
| `GET /v1/tenants/{name}` | one `TenantInfo` (404 if unknown) |
| `DELETE /v1/tenants/{name}` | **Forgets the registration only.** It deletes the `graphos_meta.tenants` row and the in-memory entry. The AGE graph and its data are kept, and re-POSTing the spec brings the tenant back. Returns `{"tenant", "status": "unregistered"}` (404 if unknown). |

While a tenant is unregistered, data-plane calls to it return 404. To remove the data itself, delete
it before you unregister: use `DELETE /v1/{tenant}/graph/nodes?property_key=…&property_value=…`, or
delete nodes one at a time.

---

## 4. Writing data

Nodes merge on `id` within their label and edges on `(src, dst, type)`; re-sending a payload is a
no-op. New property keys are added and omitted keys are kept. Edge endpoints must already exist: an
input edge with a missing endpoint is counted in `edges_skipped`, not raised. `nodes_upserted` and
`edges_upserted` count the **distinct** rows written: rows with the same `id` (or, for edges, the
same src/dst pair) in one batch collapse into one row, and the last one wins. Unregistered labels return
400 on JSON endpoints and count as invalid lines on NDJSON endpoints. Ids (`id`, `src_id`, `dst_id`)
are 1–512 characters; labels and `type` must match `^[A-Z][A-Za-z0-9_]{0,62}$`.

| Path | Endpoint | Body | Use when |
|---|---|---|---|
| Small batch | `POST /v1/{t}/graph/nodes`, `/edges` | JSON `{"nodes": [...]}` / `{"edges": [...]}`, 1–1000 rows | interactive or incremental writes |
| Property merge | `POST /v1/{t}/graph/nodes/{external_id}/properties` | `{"properties": {...}}` (at least one key) | adding values to an existing node without knowing its label (404 if the node is absent) |
| NDJSON bulk | `POST /v1/{t}/graph/nodes:bulk`, `/edges:bulk` | NDJSON, one `NodeUpsert` / `EdgeUpsert` per line | large loads; bad lines are skipped and reported |
| Snapshot import | `POST /v1/{t}/graph/snapshot-import` | NDJSON mixing nodes and edges, with `_kind` on each line | one request for a mixed file |

**Atomicity.** A small-batch call runs one transaction per label (nodes) or per
`(src_label, dst_label, type)` group (edges). The NDJSON endpoints commit in batches: 500 rows per label or edge
group for `:bulk`, and 5000 for `snapshot-import`. Each batch is its own transaction, so a stream
that fails partway leaves the earlier batches committed. Every write merges, so retrying the whole
stream is safe.

**How rows are written.** Every write endpoint (`/nodes`, `/edges`, `nodes:bulk`, `edges:bulk` and
`snapshot-import`) goes through the same SQL upsert; graphos writes no rows through Cypher. Each
batch is loaded into a staging table with binary `COPY`, then one `INSERT … ON CONFLICT` per label
or edge group merges it: `ON CONFLICT ((properties->'"id"')) DO UPDATE SET properties = properties
|| EXCLUDED.properties` for nodes, and `ON CONFLICT (start_id, end_id)` with the same property merge
for edges. There is no switch for this; it is the only write path. Node upserts rely on
`<Label>_id_unique` and edge upserts on the unique `(start_id, end_id)` index, both installed by
bootstrap. Registered tenants are re-bootstrapped at startup, so a tenant created before those
indexes existed gets them on the next restart.

### NDJSON bulk

```bash
cat > nodes.ndjson <<'EOF'
{"id": "t1", "label": "Tag", "properties": {"name": "architecture"}}
{"id": "d2", "label": "Document", "properties": {"name": "Runbook", "lang": "en", "tag_names": ["Architecture"]}}
EOF
curl -s -X POST "$G/v1/acme/graph/nodes:bulk" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/x-ndjson" \
  --data-binary @nodes.ndjson
# → {"rows_received":2,"rows_invalid":0,"nodes_upserted":2,"by_label":{"Tag":1,"Document":1},"errors":[],...}
```

Invalid lines are counted in `rows_invalid`; the first 50 are listed in `errors` as
`"line N: …"`. If the stream itself fails, the response is 400 with the generic detail
`"stream error (<ExceptionType>); see server log"` (`snapshot-import`: `"snapshot import failed (<ExceptionType>); see server log"`); database error text stays in the server log. From Python, use `g.bulk_upsert_nodes([...])` and `g.bulk_upsert_edges([...])`.

### Snapshot import

`snapshot-import` buffers NDJSON lines and groups them by label or edge key, then writes each group
with the same COPY-staged SQL upsert as the `:bulk` endpoints. A line without `_kind` is treated as a
node; a line that is not valid JSON, or not a JSON object, is skipped and listed in `errors`.

```bash
cat > snapshot.ndjson <<'EOF'
{"_kind": "node", "id": "u3", "label": "User", "properties": {"name": "Kim"}}
{"_kind": "edge", "src_id": "u3", "src_label": "User", "dst_id": "u1", "dst_label": "User", "type": "FOLLOWS", "properties": {}}
EOF
curl -s -X POST "$G/v1/acme/graph/snapshot-import" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/x-ndjson" \
  --data-binary @snapshot.ndjson
# → {"nodes_received":1,"edges_received":1,"nodes_upserted":1,"edges_upserted":1,"edges_skipped":0,...,"errors":[]}
```

Buffered nodes are flushed before every edge flush, mid-stream and at the end, so an edge never
misses an endpoint that appeared earlier in the same stream. Each 5000-row batch commits on its own:
the stream is not atomic, and a failure partway leaves the earlier batches written (re-sending the
stream is safe). A stream of more than 50,000,000 rows returns 413.

**Memory.** The audit middleware is on by default (`GRAPHOS_AUDIT_ENABLED=true`, writes only). It
reads each write request's whole body into memory to hash it, so the server does not stream `:bulk`
and `snapshot-import` bodies. Size requests with that in mind.

### Deletes

| Call | Effect |
|---|---|
| `DELETE /v1/{t}/graph/nodes/{node_id}` | Delete the node(s) with that external id and their edges, in one transaction; idempotent (`nodes_deleted: 0` if absent) |
| `DELETE /v1/{t}/graph/nodes?property_key=team&property_value=core` | Delete every node whose property equals that string, and their edges, in one transaction; returns node and edge counts (each edge once) |

---

## 5. Reads that compose

Reads identify nodes by external `id` alone. Bootstrap enforces id uniqueness only *within* a label,
so keep ids unique across the whole tenant, for example `user:42` and `doc:42`.

| Endpoint (`/v1/{t}` prefix) | Method | Key inputs (default / max) | Returns |
|---|---|---|---|
| `/graph/search` | GET | `q` (1–200 chars), `label`, `limit` (20 / 200) | `nodes[{id, label, properties}]`, `truncated`; `id` is the AGE graphid (integer), the external id is `properties.id` |
| `/graph/nodes/{external_id}` | GET | – | `label`, `properties` (404 if absent) |
| `/graph/neighbors` | GET | `node_id`, `rel_types` (repeatable), `limit` (50 / 500) | 1-hop `neighbors[{neighbor_id, neighbor_label, direction, edge_type, …}]` |
| `/graph/expand` | GET | `node_id` (always required), `seeds` (repeatable), `rel_types`, `max_hops` (2 / 5), `direction` `in\|out\|both`, `limit` (100 / 1000), `communities` | `paths[{end_id, end_label, hops, rel_path}]` |
| `/graph/induced_edges` | POST | `node_ids` (1–1000), `rel_types`, `limit` (500 / 5000) | edges within the set |
| `/graph/nodes:scan` | POST | `labels`, `where`, `project`, `order_by`, `limit` (500 / 2000), `cursor` | `items`, `next_cursor` |
| `/graph/edges:scan` | POST | `types`, `where`, `project`, `limit` (500 / 2000), `cursor` | `items[{src_id, src_label, dst_id, dst_label, type, properties}]` |
| `/graph/sample_subgraph` | POST | `label` (required), `filter_property`/`filter_value`, `rel_types`, `per_rel` (50 / 500), `include_properties`, `edge_properties` | stratified `nodes`, `edges` |
| `/graph/scoped_subgraph` | POST | see below | `nodes[{id (external id), label, properties}]`, `edges`, `truncated` |
| `/graph/export` | GET | `node_ids`, `rel_types`, `limit_nodes` (5000 / 50000) | graphology JSON |
| `/graph/catalogue` | GET | `refresh` | `by_property`: property → labels |

- **search** matches the `name` property with a case-insensitive `ILIKE '%q%'`, served by the
  trigram index `<Label>_name_trgm`. `q` is matched literally: `%` and `_` are escaped, not
  wildcards. Each hit's `id` is the
  AGE internal graphid, an integer; read the caller's external id from `properties.id`.
- **neighbors** and **expand** report the other node by external id (`neighbor_id`, `end_id`),
  with its full property bag alongside. They return an empty result, not 404, when the seed is missing. With
  `seeds`, one BFS runs from every seed; `node_id` is then ignored, but you must still send it.
  `communities` restricts end vertices to those `community_id` values. A tenant with no edge
  labels gives an empty result.
- **export** supports one format only, graphology JSON: `{attributes, options, nodes[{key,
  attributes}], edges[{key, source, target, undirected, attributes}]}`. A node's `key` is its
  external id. Its attributes add `label` (the `name` value, falling back to the id) and `_kind`
  (the vertex label).
- **sample_subgraph** takes up to `per_rel` edges of every edge type (or of `rel_types`), whatever
  the endpoint labels. `label` does not filter the sample: it names the vertex table that
  `include_properties` are read from, and endpoints outside it come back as id-only nodes. With
  `filter_property`/`filter_value`, only edges whose two endpoints are rows of `label` with that
  value are kept; the filter applies even when `include_properties` is empty (the default).

```bash
curl -s "$G/v1/acme/graph/expand?node_id=u1&seeds=u1&seeds=u2&rel_types=FOLLOWS&max_hops=2&direction=out" \
  -H "Authorization: Bearer $TOKEN"
curl -s -X POST "$G/v1/acme/graph/induced_edges" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"node_ids": ["u1", "u2", "d1"], "rel_types": ["AUTHORED", "FOLLOWS"], "limit": 1000}'
```

### nodes:scan

- `where`: `{"property", "op", "value"}` clauses combined with AND. `op` is `=`, `!=`, `in`,
  `not in`, `exists` or `not exists`. Values are compared **as text** (pass booleans as
  `"true"`/`"false"`); `in` / `not in` need a non-empty list.
- `project`: defaults to `["id"]`; `["*"]` returns the full property bag.
- `order_by`: `"id"` (default) or another property name, `"_node"` (graphid order, always indexed),
  a list of `{"property", "desc"}` clauses, or `null` for no ordering (422 if combined with `cursor`).

```bash
curl -s -X POST "$G/v1/acme/graph/nodes:scan" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"labels": ["Document"],
       "where": [{"property": "lang", "op": "=", "value": "en"},
                 {"property": "archived", "op": "not exists"}],
       "project": ["name", "lang"], "limit": 500}'
# → {"items":[{"id":"d1","label":"Document","properties":{...}}],"next_cursor":"…"|null,"scanned":…,...}
```

To page, send `next_cursor` back unchanged with the same body until it is `null`. A page also ends
early at 10 MiB of payload, and then `truncated_by_payload_cap` is true. In Python, `async for item
in g.scan_nodes_iter(labels=["Document"], project=["name"])` handles paging, and
`scan_edges_iter(types=[...])` does the same for edges.

### scoped_subgraph

Returns the vertices whose `scope_property` equals the string `scope_value`, plus the edges among
them. graphos gives no meaning to the property names. Each node is `{id, label, properties}`, where
`id` is the external id and `properties` holds only the `include_properties` keys.

| Field | Default / limit |
|---|---|
| `scope_property`, `scope_value` | required; the value must be at least 1 character |
| `group_property` + `group_values` | keep only these values (up to 64), with `node_cap` split evenly across them |
| `include_properties` | `["name"]`, up to 32 |
| `exclude_property` + `exclude_substrings` | drop vertices whose property contains any of the substrings (case-insensitive, up to 64) |
| `node_cap` / `edge_limit` | 1200 (1–4000) / 8000 (1–12000) |
| `rel_types` | optional edge-type filter |

`group_values` requires `group_property`, and `exclude_substrings` requires `exclude_property`.

```bash
curl -s -X POST "$G/v1/acme/graph/scoped_subgraph" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"scope_property": "team", "scope_value": "core",
       "group_property": "role", "group_values": ["lead", "member"],
       "include_properties": ["name", "role"],
       "exclude_property": "name", "exclude_substrings": ["bot"],
       "node_cap": 600, "rel_types": ["FOLLOWS"], "edge_limit": 4000}'
```

The scope filter is one scan over `_ag_label_vertex`. On large tenants, declare an index on the
scope property.

---

## 6. Aggregates

| Endpoint (`/v1/{t}` prefix) | Inputs (default / max) | Result |
|---|---|---|
| `POST /graph/nodes/aggregate` | `label`, `group_by`, `values`, `limit` (200 / 5000) | `groups[{value, count}]`, highest count first; a parent label includes its children |
| `POST /graph/edges/aggregate` | `group_by_src`, `group_by_dst`, `rel_types`, `src_values`, `dst_values`, `require_different`, `limit` (200 / 5000) | `groups[{src_value, dst_value, rel_type, count}]` |
| `GET /graph/labels/{label}/histogram` | `property` (required), `limit` (100 / 1000) | `bins[{value, count}]`, `n_total`, `n_distinct` (`-1` when truncated); a missing value counts as `""` |
| `GET /graph/labels/{label}/top` | `by_edge` (required), `direction` `out\|in\|both` (`out`), `k` (20 / 200) | `rows[{node_id, edge_count}]` |
| `GET /graph/stats` | – | exact per-label counts, `n_vertices`, `n_edges`, `total_bytes` |

```bash
curl -s -X POST "$G/v1/acme/graph/edges/aggregate" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"group_by_src": "team", "group_by_dst": "team",
       "rel_types": ["FOLLOWS"], "require_different": true, "limit": 1000}'
curl -s "$G/v1/acme/graph/labels/User/top?by_edge=FOLLOWS&direction=in&k=10" \
  -H "Authorization: Bearer $TOKEN"
```

`histogram` and `top` return 400 when `{label}` or `by_edge` is not registered. The Python methods
are `aggregate_nodes`, `aggregate_edges`, `label_histogram(label, property=…)`,
`label_top_by_edge(label, by_edge=…)` and `tenant_stats()`.

---

## 7. K/V cache

A per-tenant store for opaque JSON values, such as expensive aggregates. Reads check an in-process
LRU first (256 entries per replica), then `graphos_meta.cache` in Postgres.

| Call | Body / result |
|---|---|
| `PUT /v1/{t}/graph/cache/{key}` | `{"value": <any JSON>, "ttl_seconds": 1…2592000 \| null}` → `{key, expires_at, bytes_written, …}` |
| `GET /v1/{t}/graph/cache/{key}` | `{value, expires_at, cached_at, source: "lru"\|"pg"}`; 404 if missing or expired |
| `DELETE /v1/{t}/graph/cache/{key}` | 204, idempotent |

Keys are 1–256 characters; any other length returns 400. A `null` TTL means the entry never expires.

```bash
curl -s -X PUT "$G/v1/acme/graph/cache/team-matrix" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"value": {"core": {"core": 12}}, "ttl_seconds": 600}'
```

The Python methods are `cache_put(key, value, ttl_seconds=…)`, `cache_get(key)` (returns `None` on
404) and `cache_delete(key)`. To store a value on a node instead, use
`upsert_node_properties(external_id, {...})`.

---

## 8. Communities (Louvain)

Louvain is an optional extra because python-igraph is GPL-2.0-or-later: `pip install
"graphos[louvain]"`, or build the image with `--build-arg GRAPHOS_EXTRAS=louvain`. The bundled compose
file includes it unless `GRAPHOS_EXTRAS=` is set. Without it, `recompute-communities` returns **501**;
`GET /v1/info` reports availability in `features.louvain`.

`POST /v1/{tenant}/recompute-communities` requires admin. It answers when the recompute is done,
or at once as a background job with `Prefer: respond-async` (§10). The graph computation runs in a
child process, so the API keeps serving other requests meanwhile; cancelling the job terminates it.
The result depends only on the edges, not on the order rows are stored in.

| Field | Default | Rule |
|---|---|---|
| `rel_types` | all | edge types to include |
| `rel_weights` | 1.0 each | `{type: weight}` |
| `node_filter` | none | `{property: scalar \| [scalars]}`, where a scalar means `=` and a list means `IN`; both endpoints of an edge must match |
| `resolution` | 1.0 | greater than 0, at most 10 |
| `min_community_size` | 3 | at least 1; smaller communities get no id |
| `dry_run` | false | compute without writing `community_id` |

```bash
curl -s -X POST "$G/v1/acme/recompute-communities" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"rel_types": ["FOLLOWS", "AUTHORED"], "rel_weights": {"AUTHORED": 0.5},
       "node_filter": {"team": ["core", "infra"]}, "min_community_size": 3}'
# → {"n_nodes":…,"n_communities":…,"n_assigned":…,"n_singletons":…,"modularity":…,"elapsed_ms":…,...}
```

Each assigned vertex gets an integer `community_id` property. Ids carry over between runs: a new
cluster keeps the id of the previous cluster it overlaps with at Jaccard ≥ 0.5. An advisory lock
allows one recompute per tenant at a time; a concurrent call returns **409**.

Reading communities requires only tenant scope.

| Call | Notes |
|---|---|
| `GET /graph/communities?min_size=1&include_members=false&limit=50` | `limit` 0–500, where 0 means no cap; members are capped at 500 per community |
| `GET /graph/community-summary?top=10` | `top` up to 100 |
| `POST /graph/community-ids` `{"ids": [...]}` | returns `community_by_id`. Only the first 500 ids are looked up, and `truncated` is set when there were more. Unknown or unassigned ids are left out. |
| `GET /graph/expand?...&communities=4&communities=9` | BFS limited to those communities |

---

## 9. Rule-based edges (`resolve_rule`)

An edge-label entry can carry a `resolve_rule`. graphos compiles it to SQL and creates src→dst edges
by matching property values that are already stored on nodes. These are the **only** accepted keys
(`contracts/tenants.py`); any other key returns 422.

| Key | Type | Meaning |
|---|---|---|
| `src_has_array` | property | source array of scalars; set exactly one of this and `src_array_of_objects` |
| `src_array_of_objects` | property | source array of objects; requires `src_match_field` |
| `src_match_field` | property | field inside each object; only valid with `src_array_of_objects` |
| `dst_value_from` | str (required) | a property name, or `concat(arg, …)` where each arg is a property name or a `'literal'` |
| `match_op` | `eq` (default), `eq_ci` or `eq_norm` | `eq_ci` lowercases both sides; `eq_norm` lowercases, trims and collapses whitespace on both sides |
| `require_different` | list[property] | `src.p <> dst.p` for each property |
| `require_same` | list[property] | `src.p = dst.p` for each property |
| `require_dst_label` | non-empty list[label] | allowed destination labels |
| `require_dst_property` | non-empty `{property: [str, …]}` | allowed destination values |

Property names must match `^[A-Za-z_][A-Za-z0-9_]*$`. Several entries can share one edge `name`, and
all of their rules run.

```bash
curl -s -X POST "$G/v1/tenants" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data-binary @- <<'EOF'
{
  "name": "acme",
  "vertex_labels": ["User", "Document", "Tag"],
  "edge_labels": [
    "AUTHORED", "FOLLOWS",
    {"name": "TAGGED_WITH",
     "resolve_rule": {"src_has_array": "tag_names", "dst_value_from": "name",
                      "match_op": "eq_ci", "require_dst_label": ["Tag"]}},
    {"name": "WRITTEN_BY",
     "resolve_rule": {"src_array_of_objects": "authors", "src_match_field": "email",
                      "dst_value_from": "email", "require_dst_label": ["User"]}}
  ]
}
EOF

curl -s -X POST "$G/v1/acme/graph/edges:resolve" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"edge_types": ["TAGGED_WITH"]}'           # {} runs every registered rule
# → {"results":[{"edge_type":"TAGGED_WITH","edges_written":…,"duration_ms":…,"warning":null}],"total_edges_written":…,...}
```

`edges:resolve` needs tenant scope; an edge type without a rule returns 400. Inserts use
`ON CONFLICT (start_id, end_id) DO NOTHING`, so re-runs only add missing edges. Candidate labels and
supporting indexes come from the property catalogue (`GET /graph/catalogue`), which reflects the data
present at the time, so re-register after loading data to get the indexes. Python:
`g.resolve_edges(edge_types=[...])`; register rules over HTTP (`register_tenant` is typed for plain
edge-label strings).

---

## 10. Operations

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | process alive: `{"status": "ok"}`, no database access |
| `GET /readyz` | none | database round-trip: `{"status": "ready", "registered_tenants": <count>}`, or 503 with `{"status": "not_ready", "reason"}` only (`db_unreachable` on a database failure; the error goes to the server log) |
| `GET /v1/info` | none | version, `postgres_version`, `age_version`, `tenants` (a count), `features.louvain` |
| `GET /metrics` | none | Prometheus exposition; restrict it at the proxy if you expose it |
| `GET /v1/db/stats` | admin | database size, plus per-table live/dead tuples, sizes and vacuum times (top 200 tables) |
| `GET /v1/db/slow-queries?limit=25` | admin | `pg_stat_statements` top list (`limit` up to 200); `available: false` when the extension is missing |
| `POST /v1/db/vacuum` | admin | `{"tables": ["acme.User"], "full": false, "analyze": true}`. Leaving out `tables` vacuums the **whole database**. `full: true` takes exclusive locks. |

The endpoints without auth report tenant counts, never names; admins list tenants with
`GET /v1/tenants`.

### Background jobs

Community recompute, `edges:resolve` and vacuum can take minutes on a large graph. Send them with
`Prefer: respond-async` to run them as a background job: graphos validates the request exactly as
it would synchronously (so a bad body, scope or tenant still fails at once), queues it and answers
**202** with the job, `Location: /v1/jobs/{id}`, `Retry-After: 2` and
`Preference-Applied: respond-async`. Without the header the endpoints behave as before.

```bash
curl -s -X POST "$G/v1/acme/recompute-communities" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "Prefer: respond-async" -d '{"rel_types": ["FOLLOWS"]}'
# → 202 {"id": "…", "kind": "recompute_communities", "tenant": "acme", "status": "queued", …}
curl -s "$G/v1/jobs/<id>" -H "Authorization: Bearer $TOKEN"
# → {"status": "succeeded", "result": {<the body the synchronous call returns>}, …}
```

| Endpoint | Purpose |
|---|---|
| `GET /v1/jobs/{id}` | `status` (`queued`, `running`, `succeeded`, `failed`, `cancelled`); on success `result` is the synchronous response body, on failure `error` is `{"status", "detail"}`: what the synchronous call would have answered |
| `GET /v1/jobs?tenant=&status=&limit=50` | newest first |
| `POST /v1/jobs/{id}:cancel` | a queued job is cancelled at once; a running one is stopped: its database statement is cancelled and a community computation's process is terminated; 409 once finished |

A job id is a UUID. Jobs are visible to admins and to tokens with access to their tenant; vacuum
jobs, which have no tenant, to admins only; anything else answers 404. Cancelling a recompute or
vacuum job needs admin scope, as starting one does. One recompute per tenant can be queued or
running at a time; another is refused with **409**.

Every API replica runs queued jobs, `GRAPHOS_JOB_CONCURRENCY` at a time (default 1), and the
database hands each job to exactly one replica. A job whose replica stops (crash or shutdown) is
recorded as failed; jobs are never re-run automatically. Finished jobs are kept for
`GRAPHOS_JOB_RETENTION_H` hours (default 168).

**Metrics.** graphos exports `graphos_requests_total{method,route,status}`,
`graphos_request_duration_seconds{method,route}`, `graphos_db_pool_size{state=total|idle|active}`,
`graphos_registered_tenants` and `graphos_rate_limited_total{token_id}`. `route` is the path
template, not the concrete URL; `token_id` is the first 12 hex characters of the SHA-256 of the
token, never the token itself. The default `prometheus_client` process and runtime collectors are
also exported.

**Audit log.** Each audited request (writes, by default) emits one JSON log line, `write`, with
`method`, `path`, `tenant`, `consumer` (the token's consumer name, `anonymous` without one),
`args_hash`, `status` and `elapsed_ms`.

**JIT.** Off for graphos's connections (`GRAPHOS_DB_JIT=true` turns it on). On
graphos's heaviest queries it gained nothing measurable; on tenants without
statistics it spent seconds compiling queries that run in milliseconds.

**Statement timeout.** Every pooled connection runs with `statement_timeout =
GRAPHOS_STATEMENT_TIMEOUT_S` (default 60). This Postgres setting is the only per-statement limit;
the pool sets no client-side command timeout. When it fires, the response is **504** with an
`application/problem+json` body: `{"type", "title": "Query timed out", "status": 504, "detail"}`.
Writes behave the same way: `/nodes`, `/edges`, `:bulk` and `snapshot-import` also answer 504.
Narrow the request and retry. Long operations raise the limit for their own statements: tenant
registration, internal schema setup and the startup reload of stored tenants (1800 s),
`edges:resolve` (1800 s), Louvain stages (600 s, and 300 s per edge label) and vacuum (3600 s).

**Rate limiting.** Off by default. Set `GRAPHOS_RATE_LIMIT_ENABLED=true` to allow each bearer token
`GRAPHOS_RATE_LIMIT_RPS` requests per sliding window of `GRAPHOS_RATE_LIMIT_WINDOW_S` seconds
(defaults 100 and 1.0). The limiter keeps its state in memory, per replica, keyed by a SHA-256 hash
of the token (first 12 hex characters). Only tokens present in the tokens file are limited; an
unknown bearer string goes straight to auth (401) and creates no bucket and no metric series. Over
the limit, requests get **429** with a `Retry-After` header and a `{"detail", "retry_after_s"}`
body. `/healthz`, `/readyz`, `/metrics` and requests without a bearer token are never limited.

**Configuration.** Variables come from `config.py` and take the `GRAPHOS_` prefix.

| Variable | Default |
|---|---|
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` | `graphos-db` / 5432 / `graphos` / `graphos` (the compose file sets `graphos_app`) |
| `DB_PASSWORD` or `DB_PASSWORD_FILE` | one of the two is required |
| `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | 2 / 16 |
| `STATEMENT_TIMEOUT_S` | 60 |
| `BIND_HOST` / `BIND_PORT` | `0.0.0.0` / 8080 |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1`: comma-separated addresses whose `X-Forwarded-*` headers are trusted; set it to your reverse proxy |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / true |
| `AUDIT_ENABLED` / `AUDIT_ENDPOINTS_ONLY_WRITES` | true / true |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_RPS` / `RATE_LIMIT_WINDOW_S` | false / 100 / 1.0 |
| `AUTH_TOKENS_FILE` / `AUTH_DISABLED` | unset / false; startup fails when the file is unset and auth is not disabled |

As a non-superuser, the API role cannot run `LOAD 'age'` (graphos tries once per connection and
ignores the refusal), so AGE must be in the server's `shared_preload_libraries`; the compose file
sets it. At startup each stored tenant is re-bootstrapped; a tenant whose bootstrap fails is logged
and skipped, and the service starts anyway.

---

## 11. Limits and errors

Error bodies are FastAPI's `{"detail": ...}`. For 422 validation errors, `detail` is a list. The 504
timeout is the exception: it returns `application/problem+json`.

| Limit | Value |
|---|---|
| writes | small batch 1–1000 rows; ids 1–512 chars; `snapshot-import` ≤ 50,000,000 rows |
| point reads | `search` `q` 1–200 chars, `limit` ≤ 200; `neighbors` ≤ 500; `expand` hops ≤ 5, `limit` ≤ 1000 |
| set reads | `induced_edges` ≤ 1000 ids / 5000 edges; `*:scan` ≤ 2000 rows and 10 MiB per page; `scoped_subgraph` ≤ 4000 nodes / 12000 edges; `sample_subgraph` `per_rel` ≤ 500; `export` ≤ 50000 nodes |
| aggregates | `limit` ≤ 5000; histogram ≤ 1000; top `k` ≤ 200; `community-ids` first 500 (`truncated`) |
| cache | key 1–256 chars; TTL ≤ 30 days |

| Status | When |
|---|---|
| 400 | unregistered label or edge type on a write, Cypher/SQL error, bad `where` value or `cursor`, edge type without a rule, bad cache key |
| 401 | missing or malformed `Authorization` header, or unknown token |
| 403 | token lacks access to the tenant, or the endpoint needs admin |
| 404 | tenant not registered; node (`GET`, `POST …/properties`) or cache key not found; unknown tenant on `GET`/`DELETE /v1/tenants/{name}` |
| 409 | `recompute-communities` already running (or queued as a job) for the tenant; cancelling a finished job |
| 413 | `snapshot-import` over 50M rows |
| 422 | body or query validation (patterns, ranges, DDL, `resolve_rule`, `label_hierarchy`), or `order_by: null` sent with a `cursor` |
| 429 | rate limited (when enabled) |
| 501 | Louvain extra not installed |
| 503 | `/readyz` not ready |
| 504 | statement timeout on a read or a write (`application/problem+json`) |

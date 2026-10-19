# Connecting an agent (MCP)

graphos serves its reads to AI agents over the [Model Context Protocol](https://modelcontextprotocol.io)
at `/mcp`, inside the API itself: the same grants per tenant, the same caps and timeouts, and an
audit line for every call that names the person or service and the tool. The design and its
reasons are in [ADR-0002](adrs/ADR-0002-mcp-surface.md) and [ADR-0008](adrs/ADR-0008-mcp-uses-the-api-tokens.md).

## 1. Where it is

`/mcp` is on by default, on the API's own port, beside `/v1`. It needs no setting; these are
the ones that touch it:

| Variable | Default | Meaning |
|---|---|---|
| `GRAPHOS_MCP` | `true` | `false` turns `/mcp` off |
| `GRAPHOS_CYPHER` | `true` | `false` removes the `run_cypher` tool; the saved-query tools then answer 404 |
| `GRAPHOS_ALLOWED_HOSTS` | — | Set it to the public host name: `/mcp` also refuses a browser `Origin` outside it |

Behind a proxy, route `/mcp` and `/.well-known/oauth-protected-resource*` to graphos-api as
well as `/v1`, and pass the `Host` header on. Turn on the rate limiter
(`GRAPHOS_RATE_LIMIT_ENABLED=true`): agents call often. A tool returns at most 256 KiB; a longer
result is cut and says so.

## 2. Get a token

`/mcp` takes the tokens the REST API takes ([ADR-0008](adrs/ADR-0008-mcp-uses-the-api-tokens.md)).
Whatever a token may read through `/v1`, it may read through `/mcp`, and no more.

**A service account** (a program, no browser) signs in as for REST:

```bash
curl -s -X POST "$G/v1/auth/login" -H 'content-type: application/json' \
  -d '{"user": "agent", "password": "'"$SECRET"'"}'
# → {"access_token":"eyJ…","token_type":"Bearer","expires_in":900,…}
```

The account is created by an admin (`POST /v1/users` with `"kind": "service"`, UI: Access) and
granted `read` on the tenants the agent may see; its secret comes from that call or
`POST /v1/users/{name}:reset-secret`. Tokens last `GRAPHOS_ACCESS_TOKEN_TTL_S` (15 minutes by
default): a long-running program signs in again before it expires.

**A person, with single sign-on**, lets the MCP client sign them in. A call without a valid token
answers 401 with `WWW-Authenticate: Bearer resource_metadata="…"`; that metadata
(`GET /.well-known/oauth-protected-resource/mcp`) names `GRAPHOS_OIDC_ISSUER` as the
authorization server. The client signs the person in there, in a browser, and sends the
provider's access token. graphos takes it as it does on REST: the token must carry graphos's
audience (`GRAPHOS_OIDC_AUDIENCE`, or the client id), and the person's groups and roles give the
grants. The provider must know the MCP client (dynamic client registration, or a client
registered by hand) and put graphos's audience in its tokens (an audience mapper, for example).
This flow is documented, not yet tested against a real provider.

## 3. Configure the client

Any MCP client that speaks Streamable HTTP. With a service account's token, as a header:

```json
{
  "mcpServers": {
    "graphos": {
      "type": "http",
      "url": "https://graph.example.com/mcp",
      "headers": {"Authorization": "Bearer <token from §2>"}
    }
  }
}
```

With single sign-on, give the URL alone: the client finds the provider from the 401.

## 4. Tools and resources

Every tool reads, and says so (`readOnlyHint`). The list is the same for every caller and tenant:
no tool name, description or schema contains tenant data. Each tool runs the REST route it wraps,
as the caller, so it answers exactly what the route does (§ references are to [usage.md](usage.md)).

| Tool | What it reads | Route |
|---|---|---|
| `list_tenants` | the tenants the caller may read, with its level | `GET /v1/me` |
| `get_schema` | labels, counts, properties and their real values, label pairs, search indexes, saved queries | `GET /v1/{t}/schema/summary` |
| `get_graph_map` | every label with its node count, every label pair per edge type with its count | `GET …/graph/map` |
| `get_property_definitions` | the declared property definitions and their modes | `GET …/graph/properties` |
| `get_graph_stats` | counts per label, and whether metrics and communities are current | `GET …/graph/stats` |
| `recent_changes` | the latest writes, newest first: what, who, when | `GET /v1/{t}/changes/history` |
| `search_nodes` | nodes by text, or ranked through a search index | `GET …/graph/search` |
| `get_node` | one node by id | `GET …/graph/nodes/{id}` |
| `get_neighbors` | one node's edges and the nodes at their other end | `GET …/graph/neighbors` |
| `expand_subgraph` | the nodes within a few hops | `GET …/graph/expand` |
| `shortest_paths` | shortest paths between two nodes | `POST …/graph/paths` |
| `aggregate_nodes`, `get_property_values` | counts of a label's nodes by a property | `POST …/graph/nodes/aggregate` |
| `aggregate_edges` | counts of edges by a source and a target property | `POST …/graph/edges/aggregate` |
| `scan_nodes` | nodes matching property filters, by pages | `POST …/graph/nodes:scan` |
| `check_edges` | whether each of up to 1,000 edges exists | `POST …/graph/edges:check` |
| `list_communities`, `community_summary` | the communities of the last recompute | `GET …/graph/communities`, `…/community-summary` |
| `list_saved_queries`, `get_saved_query`, `run_saved_query` | the tenant's saved Cypher queries (text, description, typed parameters), one by name, run with its parameters | `…/cypher/saved[/{name}[:run]]` (needs the console on) |
| `run_cypher` | a read-only openCypher query with `$name` parameters | `POST /v1/{t}/cypher`; offered unless `GRAPHOS_CYPHER=false`, held to `GRAPHOS_CYPHER_LEVEL` |

Resources: `graphos://{tenant}/schema/summary` (the summary as text),
`graphos://{tenant}/properties` (the property definitions) and `graphos://{tenant}/saved-queries`
(the saved queries), each with a `ttlMs`.

`run_cypher` goes through the console's guard unchanged: write clauses, schema-qualified and
non-Cypher functions and variable-length patterns are refused before the database; the query runs
in a read-only transaction on the console's own pool, with its row cap, timeout and watchdog; one
query at a time per caller; its text is audited.

The schema summary is also a REST endpoint for anyone writing prompts by hand:
`GET /v1/{tenant}/schema/summary` (JSON, with the compact text in `text`) or
`?format=text`.

## 5. Security

- **Data in the graph is untrusted input to the model.** A node's text can hold instructions;
  graphos returns it as data (structured results), never in tool descriptions, but the agent
  and its user must treat it as data too.
- **Every call is audited**, reads included, whatever `GRAPHOS_AUDIT_READS` says: the audit line
  carries the consumer and `mcp_tool`.
- **Grants hold as on REST**: a tenant the caller has no grant on is refused like a missing one.
- **Reads only.** No tool writes; Cypher is read-only and only where the console is on.
- **Limits**: the routes' own caps and timeouts, the result byte cap, and the rate limiter when
  on.

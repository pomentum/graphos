# Connecting an agent (MCP)

graphos serves its reads to AI agents over the [Model Context Protocol](https://modelcontextprotocol.io)
at `/mcp`, inside the API itself: the same grants per tenant, the same caps and timeouts, and an
audit line for every call that names the person or service and the tool. The design and its
reasons are in [ADR-0002](adrs/ADR-0002-mcp-surface.md).

## 1. Turn it on

| Variable | Default | Meaning |
|---|---|---|
| `GRAPHOS_MCP` | `false` | `true` mounts `/mcp` (read tools only) |
| `GRAPHOS_MCP_RESOURCE` | — (required) | The endpoint's public URL, e.g. `https://graph.example.com/mcp`: the audience its tokens must carry |
| `GRAPHOS_MCP_MAX_RESULT_BYTES` | `262144` | A tool result longer than this is cut and says so |
| `GRAPHOS_SCHEMA_SUMMARY_TTL_S` | `300` | How long the schema summary is kept per tenant |
| `GRAPHOS_ALLOWED_HOSTS` | — | Set it to the public host name: `/mcp` also refuses a browser `Origin` outside it |

Behind a proxy, route `/mcp` and `/.well-known/oauth-protected-resource*` to graphos-api as
well as `/v1`. Turn on the rate limiter (`GRAPHOS_RATE_LIMIT_ENABLED=true`): agents call often.

## 2. Get a token for the endpoint

A token for `/mcp` must carry `GRAPHOS_MCP_RESOURCE` as its audience. A token for the REST API is
refused there, and a token for `/mcp` is refused by the REST API.

**A service account** (a program, no browser) gets one with OAuth client credentials:

```bash
curl -s -X POST "$G/v1/auth/token" \
  -d grant_type=client_credentials -d client_id=agent -d client_secret="$SECRET" \
  -d resource=https://graph.example.com/mcp
# → {"access_token":"eyJ…","token_type":"Bearer","expires_in":900,…}
```

The account is created by an admin (`POST /v1/users` with `"kind": "service"`, UI: Access) and
granted `read` on the tenants the agent may see; its secret comes from that call or
`POST /v1/users/{name}:reset-secret`. Without `resource`, the same endpoint issues a token for the
REST API.

**A person, with single sign-on**, uses an access token from the identity provider for the MCP
resource. The provider must put the MCP URL in the token's audience: with resource indicators
(RFC 8707) the client asks for it; otherwise a client scope or audience mapper adds it. MCP
clients find the provider from the endpoint's metadata:
`GET /.well-known/oauth-protected-resource/mcp` names `GRAPHOS_OIDC_ISSUER` as the authorization
server, and a call without a valid token answers 401 with
`WWW-Authenticate: Bearer resource_metadata="…"`.

## 3. Configure the client

Any MCP client that speaks Streamable HTTP and can send a header:

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

Tokens last `GRAPHOS_ACCESS_TOKEN_TTL_S` (15 minutes by default): a long-running program asks
`/v1/auth/token` again before it expires.

## 4. Tools and resources

Every tool reads, and says so (`readOnlyHint`). The list is the same for every caller and tenant:
no tool name, description or schema contains tenant data. Each tool runs the REST route it wraps,
as the caller, so it answers exactly what the route does (§ references are to [usage.md](usage.md)).

| Tool | What it reads | Route |
|---|---|---|
| `list_tenants` | the tenants the caller may read, with its level | `GET /v1/me` |
| `get_schema` | labels, counts, properties and their real values, label pairs, search indexes, saved queries | `GET /v1/{t}/schema/summary` |
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
| `list_saved_queries`, `run_saved_query` | the tenant's saved Cypher queries, run with typed parameters | `…/cypher/saved` (needs the console on) |

Resources: `graphos://{tenant}/schema/summary` (the summary as text) and
`graphos://{tenant}/properties` (the property definitions), each with a `ttlMs`.

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
- **Reads only.** Writes, and Cypher outside saved queries, are not offered over MCP.
- **Limits**: the routes' own caps and timeouts, the result byte cap, and the rate limiter when
  on.

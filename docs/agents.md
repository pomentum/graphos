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
| `GRAPHOS_ALLOWED_HOSTS` | — | Set it to the public host name: `/mcp` also refuses a browser `Origin` outside it (without it, only loopback origins) |

Behind a proxy, route `/mcp`, `/oauth/` and `/.well-known/oauth-*` to graphos-api as well as
`/v1`, and pass the `Host` header on. The bundled web UI's nginx already does: give MCP clients
the UI's address (`https://graph.example.com/mcp`), and people sign in on the UI's own screen.
Then let graphos trust the UI's scheme header (`GRAPHOS_FORWARDED_ALLOW_IPS` covering the UI
container, so it knows the client used `https`), and list the UI's host name in
`GRAPHOS_ALLOWED_HOSTS` when you set it, next to `graphos-api` (the UI's own `/api/`
requests reach graphos under that name). Turn on the rate limiter
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

**A person, without single sign-on**, lets the MCP client sign them in through graphos: no
token to paste. graphos is then the client's authorization server
(`/.well-known/oauth-authorization-server`):

1. The client calls `/mcp`, gets a 401 pointing at the metadata, and registers itself at
   `/oauth/register`. It is registered as a public client (no secret; PKCE binds each code to
   it). Its callback must be a loopback one (`http://localhost` or `127.0.0.1`, any port), as
   desktop and command-line clients use. A web client, such as a hosted chat's connector, signs
   people in through single sign-on, where the provider holds its callback.
2. It opens a browser at `/oauth/authorize`. At the web UI's address, that leads to the UI's
   `/authorize` screen: the usual sign-in (or nothing, when the person is already signed in to
   the UI), a new password when it must change, then a card that names the client (as it named
   itself) and where it sends the answer, with **Allow** and **Cancel**. At graphos-api's own
   address, graphos's sign-in page says the same and asks for the name and password. Both count
   towards the throttle and lock-out of `/v1/auth/login`.
3. The client exchanges the code (once, within 5 minutes) at `/oauth/token` for a graphos access
   token that **only reads** (the person's tenants, at `read`; every write and account change is
   refused) and a refresh token, a graphos session that belongs to that client. It renews by
   itself, at `/oauth/token` only, until the session ends (`GRAPHOS_SESSION_IDLE_S`,
   `GRAPHOS_SESSION_MAX_S`, and never after the UI session that allowed it) or is revoked
   (`/oauth/revoke`).

**A person, with single sign-on**, lets the MCP client sign them in at the provider instead;
graphos then serves no `/oauth/` endpoint. A call without a valid token
answers 401 with `WWW-Authenticate: Bearer resource_metadata="…"`; that metadata
(`GET /.well-known/oauth-protected-resource/mcp`) names `GRAPHOS_OIDC_ISSUER` as the
authorization server. The client signs the person in there, in a browser, and sends the
provider's access token. graphos takes it as it does on REST: the token must carry graphos's
audience (`GRAPHOS_OIDC_AUDIENCE`, or the client id), and the person's groups and roles give the
grants. The provider must know the MCP client (dynamic client registration, or a client
registered by hand) and put graphos's audience, and the claims the grants use, in the tokens
it issues to it.

With **Keycloak** (tried with 26.0, the MCP Python SDK's client and a browser), in the realm:

1. A client scope, `graphos`, with an **Audience** mapper (included client audience: graphos's
   client id), made a realm **default** client scope, so every client gets it, those that
   register themselves included.
2. **Client registration → anonymous access policies:** MCP clients register without a token.
   *Trusted Hosts* refuses them unless their address is listed, and *Allowed Client Scopes*
   refuses the scopes they ask for (the SDK asks for every scope the realm advertises): list
   the hosts and scopes there, or remove the two policies. *Consent Required* stays: the person
   sees Keycloak's consent page for the client.
3. A client that registers itself gets *Full Scope Allowed* off, so the person's realm roles
   reach its tokens only through its scopes: map the roles graphos's grants use into the
   `graphos` scope (its *Scope* tab), and grant them in graphos as `oidc-role:<role>` with
   `GRAPHOS_OIDC_ROLES_CLAIM=realm_access.roles`.

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

For a person, give the URL alone: the client finds graphos's sign-in, or the provider, from the
401.

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

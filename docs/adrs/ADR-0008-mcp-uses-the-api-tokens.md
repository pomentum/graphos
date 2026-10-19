# ADR-0008: `/mcp` takes the tokens the REST API takes, and is on by default

- **Status:** Accepted (2026-10-19)
- **Scope:** how `/mcp` checks tokens, its settings, `POST /v1/auth/token`, `GRAPHOS_MCP`'s default
- **Related:** ADR-0002 (MCP surface), whose §3 token rules this replaces; ADR-0006 (console on
  by default)

## Context

ADR-0002 made `/mcp` an OAuth resource of its own: a token had to carry `GRAPHOS_MCP_RESOURCE`
(the endpoint's public URL) as its audience, a token for the REST API was refused there, and the
reverse. To get such a token, a service account used a new endpoint, `POST /v1/auth/token` (OAuth
client credentials with `resource`), which brought the `python-multipart` dependency for its
form. `GRAPHOS_MCP_RESOURCE` was required to turn the endpoint on, and two more settings tuned it
(`GRAPHOS_MCP_MAX_RESULT_BYTES`, `GRAPHOS_SCHEMA_SUMMARY_TTL_S`).

Built and tried, this costs more than it protects:

- every tool runs a REST route as the caller, read-only, with the caller's grants. A token that
  opens the REST API can already read everything `/mcp` reads, and more. Refusing it on `/mcp`
  protects nothing;
- a person who signs in with a password, or a service account that signs in as it does for REST,
  has no way into `/mcp`;
- the MCP rule on audiences is there so that a server does not forward a token to another
  service. graphos forwards nothing: `/mcp` is graphos, and the REST API's audience names graphos.

## Decision

- **Tokens.** `/mcp` accepts exactly what the REST API accepts: graphos's own tokens (from
  `/v1/auth/login`, a session, or single sign-on through the UI) and, with single sign-on, the
  provider's access tokens for graphos's audience (`GRAPHOS_OIDC_AUDIENCE`, or the client id).
  There is one audience for all of graphos.
- **Discovery.** The protected-resource metadata (RFC 9728) stays at
  `/.well-known/oauth-protected-resource[/mcp]`, and a refused call still answers 401 with
  `WWW-Authenticate: Bearer resource_metadata=…`. The resource is the endpoint's URL as the
  caller reached it: the `Host` header, which the reverse proxy passes on unchanged and
  `GRAPHOS_ALLOWED_HOSTS` bounds, and the scheme, from `X-Forwarded-Proto` of a proxy in
  `GRAPHOS_FORWARDED_ALLOW_IPS`. The authorization server is `GRAPHOS_OIDC_ISSUER`. A provider used this way
  must put graphos's audience in the tokens it issues to MCP clients, as it does for REST.
- **Removed:** `POST /v1/auth/token`, its form and `python-multipart`; the settings
  `GRAPHOS_MCP_RESOURCE`, `GRAPHOS_MCP_MAX_RESULT_BYTES` (now a fixed 256 KiB) and
  `GRAPHOS_SCHEMA_SUMMARY_TTL_S` (now a fixed 300 s; the summary is still dropped when the schema
  changes).
- **On by default.** `GRAPHOS_MCP` defaults to `true`; `false` turns `/mcp` off. With nothing
  left to configure and nothing it can do that REST cannot, it follows the console (ADR-0006).
- **Origin.** `/mcp` reads `GRAPHOS_ALLOWED_HOSTS` through the same helper as the Host check, so
  the two cannot disagree (`*` turned the Host check off but refused every browser `Origin`).

## Decision options

- **Keep a separate audience for `/mcp`.** Standard for an MCP server in front of other
  services; here it only shut out the callers graphos already trusts. Left.
- **Accept both audiences** (graphos's, and an optional MCP URL). Would serve a provider that sets
  the audience from the client's `resource` parameter; nothing needs it yet, and it can be added
  as one optional setting when something does. Left for now.
- **One audience, on by default.** Taken.

## Evidence

- The tools reach data only through the REST routes, as the caller (`mcp_server._run`, lent
  tokens), so a token accepted on both reads the same on both.
- A login token was refused at `/mcp` in a live run over HTTP before this change.

## Consequences

- **Better:** an agent's configuration is the URL and a token from `/v1/auth/login`; `/mcp`
  needs no setting; one public sign-in endpoint and one dependency fewer.
- **Worse:** a provider that sets the audience only from `resource` issues tokens graphos
  refuses, until it is configured to add graphos's audience. The sign-in flow through a provider
  is documented but not tested against a real one.
- `/mcp` is a public surface on every install unless turned off. It reads only, through the
  routes, with the caller's grants, audited and rate-limited as REST.
- An install upgrading from a build with `GRAPHOS_MCP_RESOURCE` can drop it; graphos ignores it.

## Open questions

None.

## Acceptance

- A token from `/v1/auth/login` works on `/mcp`; a call without a valid token answers 401 with
  the metadata URL; `POST /v1/auth/token` answers 404.
- `Settings().mcp` is `true`; the removed settings are gone from `config.py`, the compose file,
  the README and the agents guide.

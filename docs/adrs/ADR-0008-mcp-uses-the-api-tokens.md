# ADR-0008: `/mcp` takes the tokens the REST API takes, signs its clients in, and is on by default

- **Status:** Accepted (2026-10-19)
- **Scope:** how `/mcp` checks tokens, how MCP clients sign people in, its settings,
  `POST /v1/auth/token`, `GRAPHOS_MCP`'s default
- **Related:** ADR-0002 (MCP surface), whose §3 token rules this replaces; ADR-0006 (console on
  by default)

## Context

ADR-0002 made `/mcp` an OAuth resource of its own: a token had to carry `GRAPHOS_MCP_RESOURCE`
(the endpoint's public URL) as its audience, a token for the REST API was refused there, and the
reverse. To get such a token, a service account used a new endpoint, `POST /v1/auth/token` (OAuth
client credentials with `resource`), whose form made graphos depend on `python-multipart`.
`GRAPHOS_MCP_RESOURCE` was required to turn the endpoint on, and two more settings tuned it
(`GRAPHOS_MCP_MAX_RESULT_BYTES`, `GRAPHOS_SCHEMA_SUMMARY_TTL_S`).

Built and tried, this costs more than it protects:

- every tool runs a REST route as the caller, read-only, with the caller's grants. A token that
  opens the REST API can already read everything `/mcp` reads, and more. Refusing it on `/mcp`
  protects nothing;
- a person who signs in with a password, or a service account that signs in as it does for REST,
  has no way into `/mcp`;
- the MCP rule on audiences is there so that a server does not forward a token to another
  service. graphos forwards nothing: `/mcp` is graphos, and the REST API's audience names graphos.

Accepting the REST API's tokens lets a program in, but a person would still paste a token into
their MCP client, and graphos's tokens last 15 minutes. MCP clients (desktop, command line, web)
sign people in themselves when the server names an authorization server: a browser opens, the
person signs in, and the client keeps and renews its tokens. With single sign-on that server is
the identity provider. Without it, nothing signs people in for MCP clients.

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
  `GRAPHOS_FORWARDED_ALLOW_IPS`.
- **Who signs people in for MCP clients.**
  - **With single sign-on** (`GRAPHOS_OIDC_ISSUER` set), the metadata names the identity
    provider, and the client signs the person in there. graphos checks the provider's token as on
    REST; the provider must know the client (dynamic registration, or a client registered by
    hand) and put graphos's audience in its tokens.
  - **Without it**, graphos is the authorization server (RFC 8414 metadata at
    `/.well-known/oauth-authorization-server`, its issuer the same URL as the resource's host):
    - **Endpoints** under `/oauth/`: `authorize`, `token`, `register`, `revoke`, from the MCP
      SDK's handlers, which check PKCE (S256 only), client authentication and redirect URIs.
    - **Clients register themselves** (RFC 7591). A redirect URI must be a loopback one
      (`http://localhost`, `127.0.0.1` or `[::1]`, any port: desktop and command-line clients,
      and nothing elsewhere can receive the code). A web client's callback is not served here:
      with single sign-on the identity provider, which then signs MCP clients in, holds the
      clients and their callbacks; graphos needs no setting for them. At most 1,000 clients.
      Every client
      is registered as a public one (no client secret; PKCE binds the code to it), so graphos
      keeps no client secret.
    - **The sign-in page** (`/oauth/login`) asks for a person's name and password and says which
      client asks, and where it sends the answer. It is never framed. Sign-in failures count
      towards the same throttle and lock-out as `/v1/auth/login`; a person whose password must
      change, a service account or a disabled user is refused.
    - **Codes** are 256 random bits, stored hashed, single-use and valid 5 minutes, so any replica
      can exchange them. The request between `authorize` and the sign-in page travels in the page
      and is checked again when the person signs in (the client, its redirect URI, a PKCE
      challenge), so it needs no storage either.
    - **Tokens** are graphos's own: an access token as from `/v1/auth/login`, and a refresh token
      that is a graphos session (rotation, reuse detection, idle and absolute expiry, revocation),
      bound to its client. `revoke` ends the session.
- **Removed:** `POST /v1/auth/token` and its form; graphos's direct dependency on
  `python-multipart` (the MCP SDK still brings it); the settings `GRAPHOS_MCP_RESOURCE`,
  `GRAPHOS_MCP_MAX_RESULT_BYTES` (now a fixed 256 KiB) and `GRAPHOS_SCHEMA_SUMMARY_TTL_S` (now a
  fixed 300 s; the summary is still dropped when the schema changes).
- **On by default.** `GRAPHOS_MCP` defaults to `true`; `false` turns `/mcp` and the sign-in
  endpoints off. With nothing left to configure and nothing it can do that REST cannot, it
  follows the console (ADR-0006).
- **Origin.** `/mcp` reads `GRAPHOS_ALLOWED_HOSTS` through the same helper as the Host check, so
  the two cannot disagree (`*` turned the Host check off but refused every browser `Origin`).

## Decision options

- **Keep a separate audience for `/mcp`.** Standard for an MCP server in front of other
  services; here it only shut out the callers graphos already trusts. Left.
- **Accept both audiences** (graphos's, and an optional MCP URL). Would serve a provider that sets
  the audience from the client's `resource` parameter; nothing needs it yet, and it can be added
  as one optional setting when something does. Left for now.
- **People paste tokens.** Works for a try; a 15-minute token makes it useless for daily work.
  Left.
- **graphos signs MCP clients in only without single sign-on; the provider does it otherwise.**
  Taken: each deployment has one authorization server for MCP, the one its people already use.
- **graphos always signs MCP clients in, with a "Sign in with SSO" button on its page** (graphos
  runs the provider's sign-in itself, as the UI does, and issues its own tokens). The provider
  would then need one more redirect URI and no client registration or audience mapping. More
  code; the next step if a provider makes the direct flow hard (Entra ID has no dynamic
  registration, Keycloak needs an audience mapper).

## Evidence

- The tools reach data only through the REST routes, as the caller (`mcp_server._run`, lent
  tokens), so a token accepted on both reads the same on both.
- A login token was refused at `/mcp` in a live run over HTTP before this change, and accepted
  after it, for a service account and for a person.
- The MCP Python SDK (MIT, already a dependency) ships the authorization server's handlers and
  checks; graphos supplies the storage, the sign-in page and its own tokens.

## Consequences

- **Better:** a program's configuration is the URL and a token from `/v1/auth/login`; a
  person's is the URL alone, and their client signs them in and renews; `/mcp` needs no setting;
  one public endpoint and three settings fewer.
- **Worse:** graphos is an OAuth authorization server where there is no single sign-on: a sign-in
  page, client registration open to anyone (bounded by the loopback rule, the cap and the
  consent line), and two tables. A web MCP client (one that signs in from a hosted service, such
  as a hosted chat's connector) cannot sign people in through graphos: it needs single sign-on. A provider that sets the audience only from `resource` issues
  tokens graphos refuses, until it is configured to add graphos's audience. The flow through a
  provider is documented but not tested against a real one.
- `/mcp` is a public surface on every install unless turned off. It reads only, through the
  routes, with the caller's grants, audited and rate-limited as REST.
- An install upgrading from a build with `GRAPHOS_MCP_RESOURCE` can drop it; graphos ignores it.

## Open questions

None.

## Acceptance

- A token from `/v1/auth/login` works on `/mcp`; a call without a valid token answers 401 with
  the metadata URL; `POST /v1/auth/token` answers 404.
- Without single sign-on, an MCP client registers, signs a person in on `/oauth/login`,
  exchanges the code once (a second exchange fails), calls `/mcp`, renews with the refresh token
  and revokes it; a redirect URI that is not a loopback one is refused at registration.
- With single sign-on, the metadata names the provider and `/oauth/*` is not served.
- `Settings().mcp` is `true`; the removed settings are gone from `config.py`, the compose file,
  the README and the agents guide.

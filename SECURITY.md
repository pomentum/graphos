# Security policy

## Reporting a vulnerability

Please do not open a public issue, discussion or pull request for a security problem.

Report it privately through GitHub: on the repository's **Security** tab, choose **Report a
vulnerability** ([private vulnerability reporting](https://github.com/pomentum/graphos/security/advisories/new)).
Only the maintainers see the report, and the fix can be prepared in a private fork.

A useful report says:

- the version (`GET /v1/info`, or the image tag) and the database (`postgres_version` and
  `age_version` in `GET /v1/info`);
- the settings that matter (`GRAPHOS_*` variables, SSO or passwords, the Cypher console on or off);
- the steps or the request that shows the problem, and what an attacker gains;
- whether you want to be credited in the advisory, and under which name.

We aim to acknowledge a report within a week and to agree a disclosure date with you. A fix is
released as a patch version, with a `fix(...): ... (security)` entry in the
[changelog](CHANGELOG.md) and a GitHub security advisory.

## Supported versions

graphos-api, graphos-ui and graphos-client are released together under one version. Security fixes
go into the latest minor release only.

| Version | Supported |
|---|---|
| 0.12.x | yes |
| older than 0.12 | no: upgrade to the latest release |

| Database | Supported |
|---|---|
| PostgreSQL 18 / Apache AGE 1.8 (the compose's default) | yes |
| PostgreSQL 16 / Apache AGE 1.6 | no: graphos-api does not start on it. Move to 18 ([guide](docs/upgrade-postgresql-18.md)) |

graphos is before 1.0: a minor release may change the API, and the changelog says how.

## What a deployment must do itself

graphos is meant to run behind a reverse proxy with TLS, on a private network. The checklist is
[installation.md §8, Before other people use it](docs/installation.md#8-before-other-people-use-it).
In short:

- put TLS in front of the UI and the API (both bind to `127.0.0.1` in the compose file), and list
  the proxy in `GRAPHOS_FORWARDED_ALLOW_IPS`;
- change the first admin's password at once (the first start creates `graphos` / `graphos`, whose
  tokens can do nothing until the password is changed), or start with
  `GRAPHOS_ADMIN_PASSWORD_FILE`; with single sign-on, set `GRAPHOS_LOCAL_ADMIN=false`;
- never set `GRAPHOS_AUTH_DISABLED=true` outside local development: every request is then an
  anonymous admin;
- with single sign-on, set `GRAPHOS_OIDC_REDIRECT_URIS` to the UI's callback address;
- give each program its own service account with the lowest level it needs;
- keep the master key (`GRAPHOS_MASTER_KEY_FILE`, `master_key.txt` in the compose) and back it up
  apart from the database: it seals the signing keys and ID tokens graphos stores;
- set `GRAPHOS_ALLOWED_HOSTS` to the names clients use, so a request for any other `Host` is
  refused (DNS rebinding);
- the read-only Cypher console is on by default and needs `read` on a tenant: turn it off with
  `GRAPHOS_CYPHER=false` where no one needs it, or raise `GRAPHOS_CYPHER_LEVEL`;
- the MCP endpoint `/mcp` is on by default, reads only, with the caller's grants: turn on the rate
  limiter for agents, or turn it off with `GRAPHOS_MCP=false` ([agents guide](docs/agents.md)).

## Known limits

These are known and documented; reports about them are welcome only when they show something
worse than what is written here.

- **Stored secrets are only as safe as the master key.** graphos keeps its ES256 signing keys and
  each session's ID token in `graphos_meta`, sealed with the master key
  (`GRAPHOS_MASTER_KEY_FILE`; AES-256-GCM, a key per row derived with HKDF), so a database dump
  or backup alone holds no usable key. Whoever has both the database and the master key can sign
  tokens for any user. Without a master key graphos stores them in clear, as 0.12 did, and
  warns at start. To keep the signing key out of the database altogether, mount it from a secret
  manager with `GRAPHOS_AUTH_SIGNING_KEY_FILE`
  ([installation.md §8](docs/installation.md#8-before-other-people-use-it)); grant direct SQL
  readers nothing on `graphos_meta` either way.
- **The `Host` allowlist is off by default.** With `GRAPHOS_ALLOWED_HOSTS` empty graphos answers
  any `Host` header; set it, or let the reverse proxy accept only the names it serves.
  `localhost` and `127.0.0.1` are always allowed.
- **`/metrics` needs no sign-in.** Since 0.12.0 it names no tenant, but it shows request counts
  and timings. Do not expose it beyond the monitoring network.
- **Rate limiting is off by default** (`GRAPHOS_RATE_LIMIT_ENABLED`), and both the rate limiter and
  the sign-in throttle count per replica.
- **Empty `GRAPHOS_OIDC_REDIRECT_URIS` accepts any http(s) redirect URI** from the UI; the
  identity provider's own list still applies.
- **MCP clients register themselves without signing in** (RFC 7591), where graphos signs them in
  (no single sign-on). A registration holds no secret and grants nothing: a person still signs
  in on graphos's page, which names the client and where it sends the answer. Callbacks must
  be loopback ones, and at most 1,000 clients are kept.
- **Reads by direct SQL roles are not in graphos's audit log.** Use PostgreSQL's logging
  (`log_statement`, `pgaudit`) if they must be.
- **Community detection (Louvain)** installs python-igraph from PyPI into the container at start
  when `docker-compose.louvain.yml` is used; pin or mirror it if the host must not reach PyPI.

## Dependencies and images

The images are built by the `graphos-image` workflow from this repository and published to
`ghcr.io/pomentum/graphos-api` and `ghcr.io/pomentum/graphos-ui`. The database image is
`apache/age`, pinned by digest in [`docker-compose.graphos.yml`](docker-compose.graphos.yml). A
vulnerability in a dependency that graphos does not reach is not a graphos vulnerability, but a
pull request that upgrades it is welcome.

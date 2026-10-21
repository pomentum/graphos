# Installing graphos

This guide takes graphos from nothing to a running service with the web UI, the first sign-in, users
and single sign-on, and then lists what to change before other people use it. How to use the API
once it runs is in the [usage guide](usage.md).

- [1. What you install](#1-what-you-install)
- [2. Install with Docker Compose](#2-install-with-docker-compose)
- [3. The first sign-in](#3-the-first-sign-in)
- [4. Users, service accounts and access](#4-users-service-accounts-and-access)
- [5. Single sign-on (OpenID Connect)](#5-single-sign-on-openid-connect)
- [6. Louvain communities (optional extra)](#6-louvain-communities-optional-extra)
- [7. Try it with the demo tenant](#7-try-it-with-the-demo-tenant)
- [8. Before other people use it](#8-before-other-people-use-it)
- [9. Settings for sign-in and access](#9-settings-for-sign-in-and-access)
- [10. Other ways to run it](#10-other-ways-to-run-it)
- [11. Upgrading from PostgreSQL 16](#11-upgrading-from-postgresql-16)
- [12. Upgrading from 0.4 (static tokens)](#12-upgrading-from-04-static-tokens)
- [13. Locked out](#13-locked-out)

## 1. What you install

| Part | Image | Role |
|---|---|---|
| `graphos-db` | `apache/age:release_PG18_1.8.0` (pinned by digest) | PostgreSQL 18 with Apache AGE 1.8 (graphos-api refuses older ones). On an internal network: nothing but graphos-api (and, for an upgrade, graphos-db-import) reaches it. |
| `graphos-db-export`, `graphos-db-import` | `apache/age` for PostgreSQL 16 and 18 | One-shot: move a PostgreSQL 16 database to PostgreSQL 18 on the first start after an upgrade ([§11](#11-upgrading-from-postgresql-16)); otherwise they exit at once. |
| `graphos-api` | built from `services/graphos` (published: `ghcr.io/<owner>/graphos-api`) | The REST API, sign-in and the background jobs. Listens on `127.0.0.1:8080`. |
| `graphos-ui` (optional) | built from `services/graphos-ui` (published: `ghcr.io/<owner>/graphos-ui`) | The web UI: static files and a same-origin proxy to the API, no state of its own. Listens on `127.0.0.1:8081`. |

[`docker-compose.graphos.yml`](../../docker-compose.graphos.yml) runs all of them.

**Requirements:** Docker with Compose v2, and about 4 GB of memory for Docker (the defaults are
laptop-sized; [tuning](apache-age.md) covers graphs of millions of edges). For single sign-on, an
OpenID Connect provider that graphos-api can reach over HTTPS.

## 2. Install with Docker Compose

```bash
git clone https://github.com/pomentum/graphos.git && cd graphos

# 1. Random database passwords and graphos's master key, written once to
#    services/graphos/deploy/secrets/ (gitignored)
bash scripts/init-secrets.sh            # Windows: pwsh scripts/init-secrets.ps1

# 2. Database, API and web UI (leave out --profile ui for the API alone)
docker compose -f docker-compose.graphos.yml --profile ui up -d

# 3. Up?
curl -s http://127.0.0.1:8080/readyz    # {"status":"ready",...}
```

- API: <http://127.0.0.1:8080>, with its live OpenAPI contract at `/docs`.
- UI: <http://127.0.0.1:8081> (another port: `GRAPHOS_UI_PORT=8090`).
- Consumers in other compose projects attach to the `graphos-net` network and call
  `http://graphos-api:8080`.

The data lives in the `graphos_data_pg18` volume (`graphos_data` and `graphos_upgrade` matter only
after an upgrade from PostgreSQL 16, [§11](#11-upgrading-from-postgresql-16)). `docker compose ... down`
keeps them; `down -v` deletes all three.

## 3. The first sign-in

The first start creates one user, the admin **`graphos`** with the password **`graphos`**, as
databases such as Neo4j do. graphos logs a warning at every start until it is changed, and the
change is not optional: until then, its tokens can do nothing but change the password.

**In the UI:** open <http://127.0.0.1:8081>, sign in as `graphos` / `graphos`, choose a new
password (at least 12 characters).

**With curl** (`jq` reads the answers):

```bash
G=http://127.0.0.1:8080
TOKEN=$(curl -s -X POST $G/v1/auth/login -H "content-type: application/json" \
  -d '{"user": "graphos", "password": "graphos"}' | jq -r .access_token)
ADMIN_TOKEN=$(curl -s -X POST $G/v1/me/password -H "Authorization: Bearer $TOKEN" \
  -H "content-type: application/json" \
  -d '{"current": "graphos", "new": "choose-your-own-password"}' | jq -r .access_token)
```

To start with another password instead, put it in a file and set `GRAPHOS_ADMIN_PASSWORD_FILE` (for
example `/run/graphos/secrets/admin_password.txt`; the compose file has the line, commented out)
**before the first start**: it is read only while graphos has no user. It must still be changed at
the first sign-in.

## 4. Users, service accounts and access

graphos has no static tokens. Everyone signs in and gets an access token valid 15 minutes, which
the UI and the Python client renew by themselves.

- **People** sign in to the UI (or through single sign-on, §5).
- **Service accounts** are for programs: they sign in with their name and secret. The Python client
  does it when `GRAPHOS_USER` and `GRAPHOS_PASSWORD` are set.
- **Grants** give each one a level on a tenant or a name pattern: `read`, `write`, `maintain`
  (schema, community and metrics computations) or `owner` (also gives others access). A program
  that creates its own tenants gets `create_tenants` on a pattern and owns what it creates.

```bash
# A service account; its secret is in the answer, shown once
curl -s -X POST $G/v1/users -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "content-type: application/json" -d '{"name": "loader", "kind": "service"}'
# Let it create, and so own, tenants named sales_*
curl -s -X POST $G/v1/grants -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "content-type: application/json" -d '{"subject": "user:loader", "tenant": "sales_*", "level": "create_tenants"}'
```

The UI's **Access** page does the same. The rules, levels and endpoints are in
[usage §2](usage.md#2-users-sign-in-and-access).

## 5. Single sign-on (OpenID Connect)

graphos signs people in through any OpenID Connect provider that publishes a discovery document.
The UI then shows **Sign in with SSO**, and someone already signed in at the provider is let in
without a prompt. Grants name the provider's groups, roles or users, so access follows the
directory.

**1. Register graphos at the provider** as one **public** client (no secret) using the
Authorization Code flow with **PKCE (S256)**:

| At the provider | Value |
|---|---|
| Redirect URI | the UI's address + `/auth/callback`, e.g. `https://graphs.example.com/auth/callback` |
| Post-logout redirect URI | the UI's address, e.g. `https://graphs.example.com/` |
| Scopes | `openid profile email` |
| ID token claims | a user name (`preferred_username`), and `groups` and/or roles if you grant by them |

**2. Configure graphos-api.** Add to its `environment:` in the compose file (or an override file):

```yaml
      GRAPHOS_OIDC_ISSUER: https://login.example.com/realms/acme      # exactly the discovery document's "issuer"
      GRAPHOS_OIDC_CLIENT_ID: graphos
      GRAPHOS_OIDC_REDIRECT_URIS: '["https://graphs.example.com/auth/callback"]'
      # Optional, shown with their defaults:
      # GRAPHOS_OIDC_DISPLAY_NAME: SSO                  # the button: "Sign in with SSO"
      # GRAPHOS_OIDC_SCOPES: openid profile email
      # GRAPHOS_OIDC_CONSUMER_CLAIM: preferred_username # the name shown and audited
      # GRAPHOS_OIDC_GROUPS_CLAIM: groups               # a dotted path works: realm_access.roles
      # GRAPHOS_OIDC_ROLES_CLAIM: roles
      # GRAPHOS_OIDC_AUDIENCE: graphos                  # "aud" of provider tokens programs send (default: the client id)
      # GRAPHOS_OIDC_HTTP_TIMEOUT_S: "10"
```

| Variable | Default | Meaning |
|---|---|---|
| `GRAPHOS_OIDC_ISSUER` | unset (no SSO) | The provider's issuer URL. graphos reads `<issuer>/.well-known/openid-configuration`; the value must equal its `issuer` exactly. |
| `GRAPHOS_OIDC_CLIENT_ID` | unset | graphos's client at the provider. Both this and the issuer turn SSO on. |
| `GRAPHOS_OIDC_REDIRECT_URIS` | `[]` (any http(s) URI) | The redirect URIs the UI may use, as a JSON list. **Set it in production.** |
| `GRAPHOS_OIDC_DISPLAY_NAME` | `SSO` | The sign-in button reads "Sign in with *name*". |
| `GRAPHOS_OIDC_SCOPES` | `openid profile email` | Scopes asked for. |
| `GRAPHOS_OIDC_CONSUMER_CLAIM` | `preferred_username` | Claim used as the person's name in the UI, logs and audit. |
| `GRAPHOS_OIDC_GROUPS_CLAIM` | `groups` | Claim holding the person's groups (a dotted path into nested claims works). |
| `GRAPHOS_OIDC_ROLES_CLAIM` | `roles` | Claim holding roles, e.g. `realm_access.roles`. |
| `GRAPHOS_OIDC_AUDIENCE` | the client id | The `aud` that provider access tokens sent straight to the API must carry. |
| `GRAPHOS_OIDC_HTTP_TIMEOUT_S` | `10` | Timeout of graphos-api's calls to the provider. |

graphos-api needs outbound HTTPS to the provider (discovery, keys, token endpoint); people's
browsers need to reach it too. The provider never calls graphos.

**3. Restart and grant access.** `docker compose -f docker-compose.graphos.yml --profile ui up -d`
recreates graphos-api with the new settings. A first SSO sign-in creates nothing and grants nothing:
graphos never reads the provider's directory, and a person who signs in sees only what grants to
their groups, roles or subject allow. An admin grants those with the API (the UI's Access page
lists them and removes them, and adds grants for graphos users):

```bash
curl -s -X POST $G/v1/grants -H "Authorization: Bearer $ADMIN_TOKEN" -H "content-type: application/json" \
  -d '{"subject": "oidc-group:graph-analysts", "tenant": "*", "level": "read"}'
curl -s -X POST $G/v1/grants -H "Authorization: Bearer $ADMIN_TOKEN" -H "content-type: application/json" \
  -d '{"subject": "oidc-role:graph-admin", "tenant": "*", "level": "admin"}'
```

Subjects: `oidc-group:<group>`, `oidc-role:<role>`, `oidc-sub:<subject id>` (one person),
`oidc-client:<client id>` (a program sending the provider's access tokens).

**4. Check it:** `curl -s $G/v1/auth/config` shows the issuer and client id; a private browser
window on the UI shows **Sign in with SSO**; after signing in, a reload keeps you signed in; signing
out ends the provider session too. A worked Keycloak example and a troubleshooting table are in the
[single sign-on guide](sso.md).

Passwords keep working next to SSO. Once admins sign in through the provider, set
`GRAPHOS_LOCAL_ADMIN=false` to turn off the `graphos` sign-in.

## 6. Louvain communities (optional extra)

Community detection uses python-igraph, which is GPL-2.0-or-later, so it is an optional extra:

| How you run graphos | Louvain |
|---|---|
| this compose file, built locally | included (leave it out with `GRAPHOS_EXTRAS= docker compose ...`) |
| the published image | not included; add `-f docker-compose.louvain.yml`, which installs it into the container on first start (PyPI must be reachable then): [example below](#louvain-with-the-published-images) |
| `pip install "./services/graphos[louvain]"` | included |

Everything else, the graph metrics included, works without it; `GET /v1/info` reports
`features.louvain`. What the algorithms give you is in [Graph algorithms](algorithms.md).

### Louvain with the published images

The compose file builds graphos-api and graphos-ui from your checkout. To run the published images
instead, add an override file that names them and drops the build (`!reset` needs Compose 2.24 or
later); set the version you want:

```yaml
# docker-compose.published.yml
services:
  graphos-api:
    image: ghcr.io/pomentum/graphos-api:0.13.2
    build: !reset null
  graphos-ui:
    image: ghcr.io/pomentum/graphos-ui:0.13.2
    build: !reset null
```

Then start with the Louvain overlay and the override:

```bash
docker compose -f docker-compose.graphos.yml -f docker-compose.louvain.yml \
  -f docker-compose.published.yml --profile ui up -d
curl -s http://127.0.0.1:8080/v1/info    # "features": {"louvain": true, ...}
```

On its first start graphos-api installs python-igraph into its container; restarts reuse it, and a
recreated container (a new image or changed settings) installs it again. Without
`-f docker-compose.louvain.yml`, the same override runs the published images without Louvain. If
the packages on ghcr.io are not public, sign in first with `docker login ghcr.io`.

## 7. Try it with the demo tenant

The client examples include a demo tenant: an invented open-source ecosystem of about 450 packages,
maintainers, teams and topics, made for exploring the UI and the algorithms.

```bash
pip install -e clients/python
export GRAPHOS_URL=http://127.0.0.1:8080 GRAPHOS_USER=graphos GRAPHOS_PASSWORD=<your new password>
python clients/python/examples/08_demo_ecosystem.py
```

It registers the tenant `demo`, loads it, computes the graph metrics and, with the Louvain extra,
the communities. Then open the UI, pick `demo`, and use *Explore → Graph → Colours → By group*.

![The Explore view of the demo tenant, coloured by community](../images/ui-explore-graph-dark.png)

## 8. Before other people use it

- **TLS in front.** Both ports bind to `127.0.0.1`. Put a reverse proxy with TLS in front of the
  UI (and of the API, if programs outside the host call it), and list it in
  `GRAPHOS_FORWARDED_ALLOW_IPS` so graphos sees the real client address (for sign-in throttling,
  rate limits and the audit log).
- **The admin password.** Change it at once, or start with `GRAPHOS_ADMIN_PASSWORD_FILE`. Create a
  named admin for each administrator, and with SSO set `GRAPHOS_LOCAL_ADMIN=false`.
- **Never `GRAPHOS_AUTH_DISABLED=true`.** It makes every request an anonymous admin, for local
  development only.
- **SSO redirect URIs.** Set `GRAPHOS_OIDC_REDIRECT_URIS` to the UI's callback address.
- **One service account per program**, with the lowest level it needs on its own tenants.
- **What is on by default.** The read-only Cypher console (`GRAPHOS_CYPHER=false` turns it off)
  and the MCP endpoint for agents, `/mcp` (`GRAPHOS_MCP=false`). Both only read, with the
  caller's grants. Without single sign-on, graphos also signs people in for MCP clients at
  `/oauth/`; give clients the UI's address so they sign in on the UI's own screen, and list the
  UI container in `GRAPHOS_FORWARDED_ALLOW_IPS` so graphos names its `https` address
  ([agents guide](agents.md)).
- **Signing key.** By default graphos generates its token-signing key at the first start and keeps
  it in the database, so every replica shares it. To keep the key in your secret manager
  instead, mount it as a PEM EC P-256 private key and set `GRAPHOS_AUTH_SIGNING_KEY_FILE`
  (`openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt -out
  signing_key.pem`). graphos reads it at start: to change it, replace the file and restart every
  replica. Tokens signed with the old key are then refused, and clients get new ones (the UI and
  the Python client do so by themselves). Rotating keys is your secret manager's job, and the
  identity provider's for SSO sign-ins.
- **Several deployments, one provider.** Give each its own `GRAPHOS_AUTH_ISSUER`, so a token from
  one is never accepted by another.
- **The master key.** graphos seals the secrets it stores in its database, its signing keys and
  the identity provider's ID token of each signed-in session, with the master key in
  `GRAPHOS_MASTER_KEY_FILE` (AES-256-GCM, a key per row derived with HKDF). A database dump or
  backup then holds no usable key. `scripts/init-secrets.sh` writes one (`master_key.txt`) and the
  compose sets the variable. **Back it up apart from the database:** without it graphos cannot
  open what it sealed and refuses to start. To recover from a lost key, delete the rows of
  `graphos_meta.signing_keys` (graphos makes a new key; clients sign in again) and set
  `id_token` to null in `graphos_meta.sessions`.
  - **Format:** one key per line, `<version>:<base64 of 32 bytes>`
    (`python -m graphos.vault new-key --version 2` prints one).
  - **Rotation:** add a line with a higher version, restart, run
    `python -m graphos.vault reseal` in the API container (it seals everything with the newest
    key), then remove the old line.
  - **Upgrading from 0.12 or older:** run `scripts/init-secrets.sh` once; the keys stored in clear
    are sealed at the next start.
- **Host names.** Set `GRAPHOS_ALLOWED_HOSTS` to the names clients use (`graph.example.com`, and
  `graphos-api` when the bundled web UI proxies to it). graphos then refuses (400) a request for
  any other `Host`, which stops a web page from reaching it through DNS rebinding (its address
  made to point at graphos, so the browser lets the page read the answers). `localhost` and
  `127.0.0.1` are always allowed. It matters most for the endpoints that need no sign-in, and
  for a local instance with `GRAPHOS_AUTH_DISABLED=true`.
- **Sizing and backups.** Raise the database's memory settings for large graphs
  ([tuning](apache-age.md)); back up the database, and the master key apart from it
  ([Backup and restore](#backup-and-restore)).
- **Several replicas** of graphos-api can share one database behind a load balancer
  ([usage §11](usage.md#several-replicas)).

### Backup and restore

**What to keep.** The database (`graphos`), and the master key (`master_key.txt`, or what
`GRAPHOS_MASTER_KEY_FILE` names) kept apart from it: a dump holds the signing keys and sessions
only sealed with that key. graphos keeps nothing else of its own on disk.

**Backing up.** Any PostgreSQL backup works: a snapshot of the `graphos_data_pg18` volume taken
with the database stopped, `pg_basebackup`, or a logical dump while graphos runs (one consistent
snapshot):

```bash
docker compose -f docker-compose.graphos.yml exec -T graphos-db   pg_dump -U graphos -d graphos -Fc > graphos-$(date +%F).dump
```

**Restoring a dump.** Into an empty database, then start graphos-api on it:

```bash
docker compose -f docker-compose.graphos.yml stop graphos-api
docker compose -f docker-compose.graphos.yml exec -T graphos-db createdb -U graphos graphos_restored
docker compose -f docker-compose.graphos.yml exec -T graphos-db   pg_restore -U graphos -d graphos_restored --exit-on-error < graphos-2026-10-21.dump   # serial: no -j
docker compose -f docker-compose.graphos.yml exec -T graphos-db   psql -U graphos -d graphos_restored -c "GRANT CONNECT, CREATE ON DATABASE graphos_restored TO graphos_app;"
docker compose -f docker-compose.graphos.yml exec -T graphos-db   vacuumdb -U graphos -d graphos_restored --analyze-only
```

The dump carries the app role's grants on its schemas and tables; the database-level grant is
the one to give again. Then set `GRAPHOS_DB_NAME=graphos_restored` and start graphos-api (or
restore into a new `graphos` the same way, after dropping the old one).

- **Restore serially.** With `-j`, `pg_restore` can load AGE's label catalogue before its graph
  catalogue and fail on their foreign key
  ([upgrade guide](upgrade-postgresql-18.md#a-database-you-run-yourself)).
- **AGE's catalogue.** A restore gives every tenant's schema a new OID while AGE's catalogue keeps
  the old one, and Cypher on those graphs fails ("graph with oid N does not exist") until they
  match again. graphos-api repairs this at start and logs which graphs it repaired; a role that
  may not update AGE's catalogue gets an error naming the
  [manual fix](upgrade-postgresql-18.md#a-database-you-run-yourself) instead.
- **Planner statistics** are not in a dump: the `vacuumdb --analyze-only` above gathers them, or
  the first queries are slow until autovacuum does.
- **Checking.** `GET /v1/tenants` lists the tenants, and each tenant's
  `GET /v1/{tenant}/graph/stats` its node and edge counts, to compare with the source.

### Direct SQL readers

Reports, notebooks or a DBA may read graphos's tables directly (they are plain AGE tables, see
[apache-age.md](apache-age.md) §2). Give them a role of their own that can only read, per tenant
schema, with a statement timeout. On AGE 1.8 every Cypher write from such a role is refused
("permission denied"; verified). AGE 1.6, which graphos no longer starts on, let a role with only
`SELECT` change and delete rows through Cypher `SET`, `REMOVE` and `DELETE`.

```sql
CREATE ROLE report_reader LOGIN PASSWORD '...';
ALTER ROLE report_reader SET search_path = ag_catalog, "$user", public;
ALTER ROLE report_reader SET statement_timeout = '30s';
ALTER ROLE report_reader SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE graphos TO report_reader;
-- per tenant: its schema is its name
GRANT USAGE ON SCHEMA ag_catalog, "sales" TO report_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog, "sales" TO report_reader;
-- label tables graphos creates later, for new labels
ALTER DEFAULT PRIVILEGES FOR ROLE graphos_app IN SCHEMA "sales" GRANT SELECT ON TABLES TO report_reader;
```

Grant nothing on `graphos_meta`: it holds users, grants and the token signing key. Such a reader
cannot run variable-length patterns (`-[*1..3]->`) either: AGE refuses graphos's tables for them
([apache-age.md](apache-age.md) §5), at once and harmlessly on 1.8. Reads by these roles are not
in graphos's audit log; use PostgreSQL's logging (`log_statement`, `pgaudit`) if they must be.

## 9. Settings for sign-in and access

All settings are environment variables prefixed `GRAPHOS_` ([`config.py`](../../services/graphos/src/graphos/config.py)
lists every one; the README the most common). Those for sign-in, besides the OIDC ones in §5:

| Variable | Default | Meaning |
|---|---|---|
| `GRAPHOS_ADMIN_PASSWORD_FILE` | unset | First password of the admin `graphos`, read only while graphos has no user (default: `graphos`). |
| `GRAPHOS_LOCAL_ADMIN` | `true` | `false`: the admin `graphos` cannot sign in (other admins sign in as usual). |
| `GRAPHOS_AUTH_DISABLED` | `false` | `true`: every request is an anonymous admin. Local development only. |
| `GRAPHOS_ACCESS_TOKEN_TTL_S` | `900` | Lifetime of the access tokens graphos issues (60 s to 24 h). |
| `GRAPHOS_AUTH_COOKIE_SECURE` | `true` | The web UI's session cookie is sent over HTTPS only (browsers also accept it on `http://localhost`). `false` only for a plain-HTTP deployment on another host. |
| `GRAPHOS_SESSION_IDLE_S` | `3600` | A browser session ends after this long without use. |
| `GRAPHOS_SESSION_MAX_S` | `43200` | ... and after this long in any case (12 h). |
| `GRAPHOS_LOGIN_MAX_FAILURES` | `5` | Failed sign-ins before an account is locked. |
| `GRAPHOS_LOGIN_LOCKOUT_S` | `900` | How long the lock lasts. |
| `GRAPHOS_LOGIN_ATTEMPTS_PER_MIN` | `20` | Sign-in attempts per minute from one client address. |
| `GRAPHOS_PASSWORD_MIN_LENGTH` | `12` | Shortest password a person may choose. |
| `GRAPHOS_AUTH_SIGNING_KEY_FILE` | unset | A PEM EC P-256 private key to sign tokens with, instead of the generated one (§8). |
| `GRAPHOS_MASTER_KEY_FILE` | unset (compose: `master_key.txt`) | Master keys sealing the stored signing keys and ID tokens (§8). Unset: stored in clear, with a warning. |
| `GRAPHOS_ALLOWED_HOSTS` | empty (any) | Host names graphos answers to, comma-separated; `localhost` and `127.0.0.1` always (§8). |
| `GRAPHOS_MCP` | `true` | The MCP endpoint `/mcp` for agents and, without single sign-on, graphos's sign-in for MCP clients at `/oauth/`; `false` turns both off ([agents guide](agents.md)). |
| `GRAPHOS_AUTH_ISSUER` | `graphos` | `iss` of the tokens graphos signs. |
| `GRAPHOS_RATE_LIMIT_ENABLED` | `false` | Per-user request rate limit. |
| `GRAPHOS_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted. |

## 10. Other ways to run it

- **The published images.** `ghcr.io/<owner>/graphos-api` and `ghcr.io/<owner>/graphos-ui`, tagged
  by version, take the same settings. The API needs PostgreSQL 18 with Apache AGE 1.8
  and refuses to start on an older database ([upgrade](upgrade-postgresql-18.md)), loaded with
  `shared_preload_libraries=age,pg_stat_statements`, and a database role for itself: the compose
  file and `services/graphos/deploy/db-init/` show the set-up to copy. The published API image has
  no Louvain; with the compose file, run the published images and add Louvain as in
  [§6](#louvain-with-the-published-images).
- **Without containers.** `pip install "./services/graphos[louvain]"`, point `GRAPHOS_DB_*` at the
  database, and run `python -m graphos` (see [services/graphos](../../services/graphos/README.md)).

## 11. Upgrading from PostgreSQL 16

graphos 0.9 and earlier ran PostgreSQL 16 with AGE 1.6 on the volume `graphos_data`; graphos-api
no longer starts on that database ([ADR-0007](adrs/ADR-0007-drop-postgresql-16.md)). The compose file runs PostgreSQL 18 with AGE 1.8 on a new volume,
`graphos_data_pg18`, and moves the data by itself on the first start:

```bash
git pull
docker compose -f docker-compose.graphos.yml up -d
docker compose -f docker-compose.graphos.yml logs -f graphos-db-export graphos-db-import
```

Everything moves: tenants and their graphs, users and grants, saved queries, the change feed,
revisions and sessions. `graphos_data` is only read and kept as the way back; remove it and the
dump (`docker volume rm graphos_data graphos_upgrade`) once you are content.

**[The upgrade guide](upgrade-postgresql-18.md)** has the details: what each one-shot does, how
long it takes, how to check the result, how to go back, troubleshooting, and the same move for a
database you run yourself.

## 12. Upgrading from 0.4 (static tokens)

1. Remove the tokens file and the setting that pointed at it: graphos no longer reads it.
2. Start graphos, sign in as `graphos` / `graphos` and change the password (§3).
3. For each former token, create a service account and grant it what the token had: `read` or
   `write` on its tenants; `create_tenants` on a pattern for a service that registers its own
   tenants; `admin` only where needed (§4).
4. Give each consumer its account: with the Python client, `GRAPHOS_USER` and `GRAPHOS_PASSWORD`
   (or `auth=PasswordAuth(...)`) instead of a token.

The [changelog](../../CHANGELOG.md) lists every other change.

## 13. Locked out

If no admin can sign in (the password is lost, or the last admin was disabled):

```bash
docker compose -f docker-compose.graphos.yml exec graphos-api python -m graphos reset-admin-password
```

It gives the admin `graphos` a new random password, printed once, which must be changed at the next
sign-in; it restores the user if it was disabled or deleted, and ends its sessions. A locked account
(too many failed sign-ins) opens again after `GRAPHOS_LOGIN_LOCKOUT_S`.

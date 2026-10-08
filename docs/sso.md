# Single sign-on with OpenID Connect

graphos signs people in through any OpenID Connect (OIDC) provider that publishes a discovery
document. This page says what to register at the provider and what to set in graphos. How sign-in
works and what it allows is in [usage §2](usage.md#single-sign-on-openid-connect).

---

## 1. How graphos uses OIDC

- **Authorization Code flow with PKCE (S256)**, as a **public client**: graphos holds no client
  secret.
- **graphos's API exchanges the code**, server to server. The browser only navigates to the
  provider and back; it never calls the provider's endpoints from script, so no CORS setting is
  needed, and nothing is stored in the browser.
- graphos keeps the PKCE verifier, `state` and `nonce` itself, validates the **ID token**
  (signature against the provider's published keys, issuer, audience = client id, nonce, expiry),
  and issues its own short-lived tokens.
- **Silent sign-in** (`prompt=none`) on every page load gives single sign-on to anyone with a live
  session at the provider.
- **Sign-out** uses the provider's `end_session_endpoint`, with the ID token as `id_token_hint`.
- Optionally, **programs** may send the provider's access tokens (client-credentials grant) straight
  to the API.

## 2. What to register at the provider

One application (client) for graphos:

| Setting | Value |
|---|---|
| Client type | **public** (no client secret) |
| Grant | **Authorization Code** only; PKCE required, method **S256**; no implicit grant, no password grant |
| Redirect URI | the UI's address + `/auth/callback`, e.g. `https://graphs.example.com/auth/callback` |
| Post-logout redirect URI | the UI's address, e.g. `https://graphs.example.com/` |
| Scopes | `openid profile email` |
| Code exchange | must be accepted from a server (graphos's API) without a secret, as for a public client |
| ID token claims | `sub`; a user name (`preferred_username` by default); groups and/or roles if you grant by them |

The provider must publish `<issuer>/.well-known/openid-configuration` with `issuer`,
`authorization_endpoint`, `token_endpoint`, `jwks_uri` and, for sign-out, `end_session_endpoint`,
and sign ID tokens with an asymmetric algorithm (RS256, PS256, ES256 or a stronger variant).

**Network:** graphos's API needs outbound HTTPS to the provider (discovery, keys, token endpoint);
people's browsers need to reach the provider. The provider never calls graphos.

## 3. What to set in graphos

```bash
GRAPHOS_OIDC_ISSUER=<the issuer URL>
GRAPHOS_OIDC_CLIENT_ID=<the client id>
GRAPHOS_OIDC_REDIRECT_URIS=["https://graphs.example.com/auth/callback"]
# Optional
GRAPHOS_OIDC_DISPLAY_NAME=SSO                 # the button reads "Sign in with SSO"
GRAPHOS_OIDC_SCOPES=openid profile email
GRAPHOS_OIDC_CONSUMER_CLAIM=preferred_username
GRAPHOS_OIDC_GROUPS_CLAIM=groups              # a dotted path works: realm_access.roles
GRAPHOS_OIDC_ROLES_CLAIM=roles
GRAPHOS_OIDC_AUDIENCE=<aud of access tokens programs send; default: the client id>
GRAPHOS_OIDC_HTTP_TIMEOUT_S=10
```

`GRAPHOS_OIDC_ISSUER` must be **exactly** the `issuer` value of the discovery document (scheme, host,
path, trailing slash or not):

```bash
curl -s <issuer>/.well-known/openid-configuration | jq -r .issuer
```

## 4. Giving access

A first sign-in creates nothing: without a grant a person signs in and sees no tenant. graphos never
reads the provider's directory. An admin grants levels to the provider's identities with the API
(the UI's Access page lists these grants and removes them, and adds grants for graphos users):

| Subject | Matches |
|---|---|
| `oidc-group:<group>` | everyone whose groups claim contains it |
| `oidc-role:<role>` | everyone whose roles claim contains it |
| `oidc-sub:<sub>` | one person (the provider's stable subject id) |
| `oidc-client:<client id>` | a program sending the provider's access tokens (`azp` / `client_id`) |

```bash
curl -s -X POST "$G/v1/grants" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"subject": "oidc-group:graph-analysts", "tenant": "*", "level": "read"}'
curl -s -X POST "$G/v1/grants" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"subject": "oidc-role:graph-admin", "tenant": "*", "level": "admin"}'
```

Groups and roles are read from the ID token, and from the provider's access token when it is a JWT
for graphos's audience. They are taken at sign-in: a change at the provider applies at the next
sign-in, a grant change in graphos applies at once.

**Programs.** A program holding provider credentials sends the provider's access token as the
bearer. Its `aud` must be `GRAPHOS_OIDC_AUDIENCE` (default: the client id), and its grants come
from `oidc-client:<its client id>` (and its subject, groups and roles). The Python client takes
such tokens with `BearerAuth(source)`.

## 5. Checking it works

1. `curl -s $G/v1/auth/config` shows the issuer and client id.
2. Open the UI in a private window: it goes to the provider and back without a prompt, then shows
   the sign-in page with **Sign in with SSO**.
3. Sign in at the provider: back in graphos, on a tenant your grants allow.
4. Reload: signed in again without a prompt (single sign-on).
5. Sign out: back to the sign-in page, and a reload does not sign you in again.

## 6. When it does not work

| Symptom | Likely cause |
|---|---|
| graphos logs `single sign-on: … names issuer …` at start | `GRAPHOS_OIDC_ISSUER` differs from the discovery document's `issuer`. |
| The provider reports an invalid redirect URI | The UI's `/auth/callback` is not registered exactly, or not in `GRAPHOS_OIDC_REDIRECT_URIS`. |
| graphos logs *the identity provider refused the code* | The client is confidential (wants a secret), PKCE is not enabled, or the provider only accepts code exchanges from a browser. |
| Signed in, but no tenant | No grant matches: compare the ID token's groups/roles claims with `GET /v1/grants`. |
| Groups or roles missing | They are not added to the **ID token**, or the claim name differs from `GRAPHOS_OIDC_GROUPS_CLAIM` / `GRAPHOS_OIDC_ROLES_CLAIM`. |
| The provider asks to confirm sign-out | It received no `id_token_hint`: sessions started before graphos kept the ID token; a new sign-in fixes it. |
| *the identity provider could not be reached* | No outbound access from graphos's API to the provider, or the timeout is too short. Passwords keep working meanwhile. |
| Programs' provider tokens get 401 | Their `aud` is not `GRAPHOS_OIDC_AUDIENCE`, or they come from another issuer. |

---

## Example: Keycloak

The setup used to test graphos's single sign-on (Keycloak 26).

- **Issuer:** `https://<keycloak host>/realms/<realm>`
- **Client:** *Client authentication* off, *Standard flow* on, *Direct access grants* off; *Valid
  redirect URIs* `https://graphs.example.com/auth/callback`; *Valid post logout redirect URIs*
  `https://graphs.example.com/*`; *Web origins* empty; *Advanced → PKCE method* `S256`.
- **Groups:** a *Group Membership* mapper on the client's dedicated scope, claim `groups`, *Full
  group path* off, *Add to ID token* on.
- **Roles:** realm roles arrive as `realm_access.roles`: set
  `GRAPHOS_OIDC_ROLES_CLAIM=realm_access.roles`.

As an importable client:

```json
{
  "clientId": "graphos", "publicClient": true, "standardFlowEnabled": true,
  "implicitFlowEnabled": false, "directAccessGrantsEnabled": false,
  "redirectUris": ["https://graphs.example.com/auth/callback"], "webOrigins": [],
  "attributes": {"pkce.code.challenge.method": "S256",
                 "post.logout.redirect.uris": "https://graphs.example.com/*"},
  "protocolMappers": [{
    "name": "groups", "protocol": "openid-connect",
    "protocolMapper": "oidc-group-membership-mapper",
    "config": {"full.path": "false", "id.token.claim": "true",
               "access.token.claim": "true", "claim.name": "groups"}
  }]
}
```

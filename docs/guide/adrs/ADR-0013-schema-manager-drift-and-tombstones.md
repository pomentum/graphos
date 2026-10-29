# ADR-0013: A tenant's schema manager, drift reports, and drops that stay dropped

- **Status:** Proposed (2026-10-29)
- **Scope:** tenant schema writes (`POST /v1/tenants`, `schema:rename`, label drops, pair
  removal), the Python client, the web UI's schema editor
- **Related:** concept.md §2.3, usage.md (registration, dry run), [ADR-0009](ADR-0009-rename-and-deprecate.md)
  (renames, deprecation, drops)

## Context

A tenant's schema has two kinds of writer:

- **a program** that declares the schema from its code and registers it on every start
  (`POST /v1/tenants`, merge mode; the client's examples and `GraphosPool` do this);
- **people** in the web UI's schema editor, which writes through the same merge endpoint.

Neither knows about the other. Found in the code (`bootstrap.persist_spec`, `by_entry.merge`,
`api/tenants.py`, `graphos-ui/src/api/graphosApi.ts`, `graphos_client._client`, `_pool`):

| # | Case | What happens |
|---|---|---|
| 1 | The program sends an entry people changed in the UI: a label's style (`display`), a property definition, a deprecation, a `meta` key, a search index, `strict_endpoints` | A re-sent entry replaces the stored one whole: every start of the program silently puts its version back. |
| 2 | People add labels, edge types, pairs or properties in the UI | Kept (a merge only adds). The program never learns of them. |
| 3 | People rename a label or edge type | The program's next registration still names the old name and is refused, 409, whole request; `GraphosPool` raises it and the program fails to start. |
| 4 | People drop a deprecated label, or remove a declared pair | Nothing remembers it: the program's next registration creates it again, silently. |
| 5 | Either side registers with `force_shrink`, or the UI's import "Replace" | The other side's additions and entries are gone. |

Two people editing in the UI could also overwrite each other (the editor writes whole entries
from a cached copy). That is fixed apart from this ADR, and comes first: graphos serves the
schema's version as an `ETag` and schema writes take `If-Match` (412 when stale), as node and
edge writes do. That fix is a precondition on existing endpoints and needs no ADR; this one
builds on it.

What is left is a question of authority: when a program declares the schema in code, which writer
is right?

## Decision

Three parts, each usable alone.

### 1. A schema manager per tenant (opt-in)

A tenant may name one **schema manager**: a subject, as grants name them (`user:loader`).

- **Set** by an owner of the tenant: at registration with `"manage_schema": true`, which makes
  the caller the manager (a program creating its own tenant is its owner), or with
  `PUT /v1/tenants/{name}/schema-manager` `{"subject": "user:loader"}`. **Released** by an owner
  with `DELETE /v1/tenants/{name}/schema-manager`. Both audited, both a schema change record.
- **Stored** apart from the spec, in `graphos_meta.tenant_settings` (`schema_manager`,
  `schema_manager_since`), for the reason the other settings are there: a server that predates
  it rewrites the spec on re-registration and must not drop it.
- **Shown** in `GET /v1/tenants/{name}` (`schema_manager`) and the exported schema document's
  header, and in the schema summary agents read.
- **On a managed tenant, the structure is the manager's.** Anyone else's schema write that
  would change anything but `display` is refused with 409, `code: schema_managed`, naming the
  manager: registrations (judged on the merged result, so re-sending what is stored is not a
  change), `schema:rename`, label drops, pair removals, tombstone clearing. The manager needs
  the access level each write needs anyway: being the manager grants nothing, it only keeps
  others out.
- **Styles stay people's.** `display` entries are presentation. On a managed tenant anyone with
  `maintain` may change them, and the manager's registration sets only the `display` entries
  that do not exist yet: a program supplies default styles, people adjust them, and the next
  start keeps the adjustment. (On an unmanaged tenant, `display` merges as today.)
- **The UI** shows a managed tenant's structure read-only, with a banner naming the manager and
  saying where changes are made; style controls stay. Owners see *Release* (its confirmation
  says the program's next registration then merges as on any unmanaged tenant, and may overwrite
  what people change) and *Transfer*.
- An owner or admin has no silent bypass: they release the tenant (audited) and then edit.

### 2. A drift report

- **Server.** The dry run of a registration (`?dry_run=true`) gains `drift`:
  - `not_declared`: what the tenant has that the body does not mention (labels, edge types,
    pairs, and the `display`, `property_schema`, `deprecated` entries, search indexes);
  - `overwrites`: entries the body would replace with a different value (part and name);
  - `refused`: names the body uses that were renamed or dropped, with where a rename went.

  It is meaningful for a body that declares a whole schema, which is what a program sends.
- **Client.** `GraphosClient.ensure_tenant(spec, on_drift="warn" | "fail" | "ignore")` runs the
  dry run, logs the drift (`graphos_client.drift`, one line per item), raises
  `SchemaDriftError` on `fail` or on a refusal, then registers. With `"warn"` a program learns
  at start what people changed instead of undoing it or failing on a bare 409. `GraphosPool`
  uses it with `"warn"`. `register_tenant` is unchanged.

### 3. Drops that stay dropped (tombstones)

- Dropping a label or edge type, and removing a declared pair, record a **tombstone** in
  `graphos_meta.schema_tombstones` (tenant, kind, name or pair, when, by whom), in the same
  transaction.
- A registration that names a tombstoned label, edge type or pair is refused, 409,
  `code: schema_dropped`, naming them, as a registration naming a renamed name already is
  (ADR-0009). The dry run lists them under `drift.refused`.
- `GET /v1/tenants/{name}/dropped` lists them; `DELETE /v1/tenants/{name}/dropped` with
  `{"label": ...}`, `{"edge_type": ...}` or `{"edge_endpoint": [type, src, dst]}` clears one
  (owner; on a managed tenant, the manager). Deleting a tenant with its data removes its
  tombstones.
- Only drops made after this change leave a tombstone; nothing is inferred for earlier ones.

## Decision options

| Option | Taken? | Why |
|---|---|---|
| **A manager per tenant, opt-in; styles stay people's** | Yes | One writer of structure where a program owns it, today's shared editing elsewhere; small to build on the schema version. |
| **A drift report in the dry run and the client** | Yes | Costs little, and turns silent overwrites and bare 409s into a readable list, managed or not. |
| **Tombstones for drops and pair removals** | Yes | The same rule as renames: a removal a person made is not undone by an older copy of the program. |
| Per-entry field managers (as Kubernetes server-side apply): each writer applies its full set, graphos records who owns each entry, conflicts are 409 unless forced, entries a writer stops declaring are removed | No, for now | Solves more (several programs on one tenant, removals by omission) but changes "a schema only grows" (ADR-0009), needs provenance on every entry and a conflict UI. Worth it only if several programs share one tenant's schema. |
| Programs only: no schema editing in the UI | No | Many tenants have no program; people design schemas in the UI. |
| A fixed split without a manager (programs own structure, people own styles, everywhere) | No | Wrong for tenants that people maintain: they would lose structure editing. |
| A read-only flag without a named manager | No | Says nothing about who may write, so the program itself would be refused too. |
| Drift report alone | No | Makes drift visible but keeps last-writer-wins on structure. |
| A dropped name in a registration skipped with a warning instead of refused | Open | Kinder to old programs, but silent; refusing matches renames. See open questions. |

## Evidence

- `bootstrap.persist_spec` merges in one `INSERT … ON CONFLICT DO UPDATE`: labels, types,
  indexes, columns, hierarchy and rules by union; `display`, `property_schema`, `deprecated`
  per entry, an entry sent again replacing the stored one whole (`by_entry.merge`); pairs
  added, never removed.
- The UI's schema edits are all partial `POST /v1/tenants` (`addToSchema`, deprecations,
  property definitions, strict endpoints, pairs, styles); the client's `register_tenant` is the
  same call and always sends labels, types, indexes, columns, hierarchy and `meta`.
- `schema_rename` records renames in the spec; a registration naming an old name is refused
  (`renames.py`). `drop_label` and pair removal record nothing.
- `GraphosPool.is_schema_drift` recovers only from "not registered" / "does not exist" (400,
  404); a 409 is raised.
- `TENANT_ROWS_SQL` already computes a version of each tenant's registration (md5 of the spec,
  the pairs and the settings) for replica sync; the separate fix serves it as the `ETag`. The
  manager and the tombstones are added to it.

## Consequences

- A program can own its schema: people see why they cannot change its structure and where to
  change it; its styles stay theirs; its start reports what differs instead of undoing it.
- Unmanaged tenants behave as today, apart from tombstones: a program that still declares a
  label someone dropped after this change is refused (409) until its code drops it too or an
  owner clears the tombstone. That is a behaviour change for such programs, the same one
  renames brought; the drift report names it.
- A rolling upgrade or a rollback runs servers that do not know the manager or tombstones: while
  they serve, edits are not refused and drops can come back. Documented; the state is kept.
- Contracts: `TenantInfo.schema_manager`, the registration's `manage_schema`, the dry run's
  `drift`, the `schema-manager` and `dropped` endpoints, the codes `schema_managed` and
  `schema_dropped`. The Python client mirrors them (`ensure_tenant`, `SchemaDriftError`,
  manager and tombstone methods); the UI's schema editor, Name changes and Access views change.
- Docs: usage (registration, dry run, the new endpoints), concept §2.3, features, the UI's
  screenshots of the schema editor.

## Open questions

- A dropped name in a registration: refused (as proposed, like renames) or skipped with a
  warning?
- Which parts besides `display` are presentation on a managed tenant: `meta`? the tenant's
  `community_algorithm`?
- Should a manager be able to be a group or role from single sign-on, or only a user?

## Acceptance

- DB tests: a managed tenant refuses others' structure changes (each write path) and accepts
  their style changes; the manager's registration keeps people's styles and adds missing ones;
  release and transfer; tombstones refuse re-registration and clear; the dry run's `drift` on
  each case of the table in Context.
- Client tests: `ensure_tenant` warns, fails and registers as specified; `GraphosPool` warns.
- UI: the read-only state, the banner, release and transfer; screenshots retaken.
- Docs as listed in Consequences; CHANGELOG.

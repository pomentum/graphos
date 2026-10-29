# ADR-0013: A tenant's schema manager, and drift reports

- **Status:** Proposed (2026-10-29); revised (2026-10-29): drops and pair removals are reported in
  the drift report instead of refused (tombstones with 409 dropped), and the precedents recorded;
  built (2026-10-29) without new routes (the manager is a registration field); **amended by
  [ADR-0014](ADR-0014-schema-manager-after-review.md)** after an independent review: styles merge
  as the registration says (`display_merge`), the community algorithm is not structure, the drift
  report covers every part, single-sign-on callers can manage, the manager survives unregister.
  Where this ADR and ADR-0014 differ, ADR-0014 holds. Accepted after the trial in *Acceptance*.
- **Scope:** tenant schema writes (`POST /v1/tenants`, `schema:rename`, label drops, pair
  removal), the Python client, the web UI's schema editor
- **Related:** concept.md §2.2, usage.md (registration, dry run, *Schema versions and `If-Match`*),
  [ADR-0009](ADR-0009-rename-and-deprecate.md) (renames, deprecation, drops)

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
from a cached copy). That is fixed apart from this ADR, in 0.14.6: graphos serves the schema's
version as an `ETag` and schema writes take `If-Match` (412 when stale), as node and edge writes
do. This ADR builds on it.

What is left is a question of authority: when a program declares the schema in code, which writer
is right?

## Decision

Three parts, each usable alone.

### 1. A schema manager per tenant (opt-in)

A tenant may name one **schema manager**: a user or service account, as grants name them
(`user:loader`). *(ADR-0014: also a person signed in through single sign-on, `oidc-sub:<sub>`, and
`"self"` for the caller.)*

- **Set, transferred or released** by an owner of the tenant through the registration itself, a
  field like `strict_endpoints`: `"schema_manager": "user:loader"` sets or transfers it, `null`
  releases it, and leaving it out leaves it as it is. A program creating its own tenant is its
  owner, so it names itself in its first registration. No route of its own: setting the manager
  is a schema write like the others (audited, a schema change record, `If-Match`).
- **Stored** apart from the spec, in `graphos_meta.tenant_settings` (`schema_manager`,
  `schema_manager_since`), for the reason the other settings are there: a server that predates
  it rewrites the spec on re-registration and must not drop it. It joins the schema version
  only when set, so a tenant that never had a manager keeps its `ETag` across the upgrade. Unregistering a tenant forgets
  its manager, as it forgets the tenant's other settings. *(ADR-0014: it keeps it; restoring the
  tenant restores its manager.)*
- **Shown** in `GET /v1/tenants/{name}` and `GET /v1/{tenant}/schema` (`schema_manager`). Not in
  the exported schema document: importing it into another tenant must not make anyone its
  manager.
- **On a managed tenant, the structure is the manager's.** Anyone else's schema write that
  would change anything but `display` is refused with 409, `code: schema_managed`, naming the
  manager: registrations (judged on the merged result, so re-sending what is stored is not a
  change), `schema:rename`, label drops, pair removals. The manager needs the access level each
  write needs anyway: being the manager grants nothing, it only keeps others out.
- **Styles stay people's.** `display` entries are presentation. On a managed tenant anyone with
  `maintain` may change them, and the manager's registration (by default, ADR-0014) sets only the `display` entries
  that do not exist yet: a program supplies default styles, people adjust them, and the next
  start keeps the adjustment. (On an unmanaged tenant, `display` merges as today.) `meta` and the
  tenant's `community_algorithm` stay the manager's. *(ADR-0014: the community algorithm is a
  setting anyone with `maintain` may change; `meta` stays the manager's.)*
- **The UI** shows a managed tenant's structure read-only, with a banner naming the manager and
  saying where changes are made; style controls stay. Owners see *Release* (its confirmation
  says the program's next registration then merges as on any unmanaged tenant, and may overwrite
  what people change). Transferring is the registration field, for an owner's program or script.
- An owner or admin has no silent bypass: they release the tenant (audited) and then edit.

### 2. A drift report

- **Server.** The dry run of a registration (`?dry_run=true`) gains `drift`:
  - `not_declared`: what the tenant has that the body does not mention (labels, edge types,
    pairs, and the `display`, `property_schema`, `deprecated` entries, search indexes);
  - `overwrites`: entries the body would replace with a different value (part and name);
  - `recreates`: labels, edge types and pairs the body declares that were dropped or removed,
    with when and by whom (part 3).

  A body naming a renamed name is refused as before (409, ADR-0009), in the dry run too, so it
  needs no list of its own. The report is meaningful for a body that declares a whole schema,
  which is what a program sends.
- **Client.** `GraphosClient.ensure_tenant(..., on_drift="warn" | "fail" | "ignore")`, with
  `register_tenant`'s keywords, runs the dry run, logs the drift (`graphos_client.drift`, one
  line per item), raises `SchemaDriftError` with `"fail"` when something would be overwritten or
  created again, then registers. A refusal (409: a renamed name, or `schema_managed`) is the
  client's usual error, which now carries the body's `code` and `schema_manager`. With `"warn"`
  a program learns at start what people changed instead of undoing it unseen. `GraphosPool`
  uses it with `"warn"`. *(ADR-0014: `GraphosPool` takes `on_drift`, `"ignore"` by default.)* `register_tenant` keeps its behaviour and gains `schema_manager=` and
  `manage_schema=True` (the caller's own `user:<name>`).

### 3. Drops and pair removals: recorded and reported, not refused

- Dropping a label or edge type, and removing a declared pair, are recorded in
  `graphos_meta.schema_removals` (tenant, kind, name or pair, when, by whom), in the same
  transaction.
- A registration that declares a removed label, edge type or pair **creates it again, as today**:
  what the program declares wins. The dry run's drift report lists it under `recreates`, so the
  program's operator learns of it and either drops it from the code or accepts it back. Nothing is
  refused.
- On a managed tenant only the manager drops or removes, so the case arises only when its own
  older code still declares what it removed.
- Renames keep ADR-0009's refusal (409): a renamed label's rows live on under the new name, and
  creating the old name again would split one label's data across two tables. A drop has no
  rows to split.
- The records are read only by the dry run's drift report (the change feed already shows each
  drop); a record goes when its item is registered again, and all of them with the tenant's
  data.

## Decision options

| Option | Taken? | Why |
|---|---|---|
| **A manager per tenant, opt-in; styles stay people's** | Yes | One writer of structure where a program owns it, today's shared editing elsewhere; the pattern of provisioned dashboards (Grafana) and migration-managed metadata (Hasura). |
| **A drift report in the dry run and the client** | Yes | Turns silent overwrites and bare 409s into a readable list, managed or not; what `terraform plan` does for infrastructure changed outside Terraform. |
| **Drops and pair removals reported, the declared schema winning** | Yes | The usual rule of declarative tools: what the code declares is applied; changes made elsewhere are shown, not enforced against the code. |
| Drops kept as tombstones that refuse a later registration naming them (409) | No (first draft) | No precedent found, and against the declared-state-wins rule: a program's start would fail for a reason it cannot see. On a managed tenant it is hardly needed, since only the manager drops. |
| Per-entry field managers (as Kubernetes server-side apply): each writer applies its full set, graphos records who owns each entry, conflicts are 409 unless forced, entries a writer stops declaring are removed | No, for now | Solves more (several programs on one tenant, removals by omission) but changes "a schema only grows" (ADR-0009), needs provenance on every entry and a conflict UI. Worth it only if several programs share one tenant's schema. The fixed split (structure to the manager, styles to people) is its simplest case. |
| Programs only: no schema editing in the UI | No | Many tenants have no program; people design schemas in the UI. |
| A fixed split without a manager (programs own structure, people own styles, everywhere) | No | Wrong for tenants that people maintain: they would lose structure editing. |
| A read-only flag without a named manager | No | Says nothing about who may write, so the program itself would be refused too. |
| Drift report alone | No | Makes drift visible but keeps last-writer-wins on structure. |

## Evidence

**In graphos** (the code, 0.14.6):

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
- The schema version (0.14.6, `graphos.schema_version`) is the md5 of the spec, the pairs and
  the settings; the manager is added to it while set (the removal records are not part of it).

**Precedents** (checked 2026-10-29; the Kubernetes and Terraform points come from third-party
write-ups, not their official documentation):

| Part | Precedent |
|---|---|
| One manager owns the structure; the UI is read-only for it | **Grafana provisioning:** a dashboard provisioned from files cannot be saved from the UI unless the provider sets `allowUiUpdates`, off by default ([commit adding the option](https://forger.sitiv.fr/apitech/grafana/commit/a45ce365667470725043c6b5053f9415b0eeb53e); [community thread](https://community.grafana.com/t/dashboard-provisioning-cannot-save-provisioned-dashboards/31175)); deleting one from the UI is a setting of its own ([thread](https://community.grafana.com/t/why-cant-i-delete-provisioned-dashboard-from-ui/132999)). **Hasura:** with migrations, the console served by the server is to be turned off, or changes made there are not tracked ([migrations setup](https://hasura.io/docs/latest/graphql/core/migrations/migrations-setup/)), and version control is the source of truth for metadata ([metadata best practices](https://hasura.io/docs/latest/deployment/best-practices/metadata/)). |
| Styles stay editable: ownership split by field | **Kubernetes server-side apply** records the manager of each field; applying a field another manager owns is a conflict unless forced, and a field managed elsewhere (an autoscaler's `replicas`) is left out of the manifest ([D2iQ](https://eng.d2iq.com/blog/conflict-resolution-kubernetes-server-side-apply/); [OneUptime](https://oneuptime.com/blog/post/2026-02-09-server-side-apply-field-ownership/markdown)). **Terraform** `lifecycle.ignore_changes` tolerates named attributes changed outside it ([KodeKloud](https://notes.kodekloud.com/docs/Terraform-On-Azure/Lifecycle-Meta-Arguments/ignore-changes/page.md)). |
| Drift report | **Terraform** finds changes made outside it when it plans or refreshes, and teams run scheduled plans to report drift ([Firefly](https://www.firefly.ai/academy/terraform-lifecycle); [Encore](https://encore.dev/articles/terraform-drift)). |
| Declared state wins over removals made elsewhere | The same tools: a plan applies what the code declares, so an attribute or resource changed or removed outside is put back unless the code says otherwise (`ignore_changes`, or removing it from the code). No precedent was found for refusing the code's declaration instead, which is why the first draft's tombstones were dropped. |

## Consequences

- A program can own its schema: people see why they cannot change its structure and where to
  change it; its styles stay theirs; its start reports what differs instead of undoing it.
- Unmanaged tenants behave as today; the drift report and the removal records only add
  information. No registration that works today is refused by this ADR.
- A rolling upgrade or a rollback runs servers that do not know the manager: while they serve,
  edits are not refused. Documented; the state is kept.
- Contracts, no new routes: the registration's `schema_manager` field, `schema_manager` in
  `GET /v1/tenants/{name}` and `GET /v1/{tenant}/schema`, the dry run's `drift`, and the 409
  `code: schema_managed`. The Python client mirrors them (`schema_manager=` and
  `manage_schema=True` on its registrations, `ensure_tenant`, `SchemaDriftError`); the UI's
  schema editor shows the banner and keeps structure controls disabled for others.
- A program that registers its schema on every start adopts it by naming itself manager in a
  registration (its service account owns the tenant) and calling `ensure_tenant` instead of
  `register_tenant`.
- Docs: usage (registration, the dry run's drift, the schema manager), concept §2.2, features;
  the UI's screenshots of the schema editor later.

## Open questions

- Which parts besides `display` are presentation on a managed tenant. Proposed: none (`meta` and
  `community_algorithm` stay the manager's). *(Decided in ADR-0014: the community algorithm is
  not structure.)*
- Whether a manager may be a group or role from single sign-on. Proposed: users and service
  accounts only; a program is a service account. *(Decided in ADR-0014: a single-sign-on
  subject may manage; groups and roles may not.)*

## Acceptance

- DB tests: a managed tenant refuses others' structure changes (each write path) and accepts
  their style changes; the manager's registration keeps people's styles and adds missing ones;
  release and transfer; a drop is recorded and a later registration recreates it with
  `recreates` in its dry run; the dry run's `drift` on each case of the table in Context.
- Client tests: `ensure_tenant` warns, fails and registers as specified; `GraphosPool` warns.
- UI: the read-only state, the banner and release (transferring is the registration field); screenshots retaken.
- A trial with one program that registers its schema on every start, as the first managed
  tenant, before the status moves to Accepted.
- Docs as listed in Consequences; CHANGELOG.

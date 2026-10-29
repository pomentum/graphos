# ADR-0014: The schema manager, after an independent review

- **Status:** Accepted (2026-10-29)
- **Scope:** ADR-0013's schema manager and drift report: server, Python client, web UI, docs
- **Related:** [ADR-0013](ADR-0013-schema-manager-and-drift-reports.md) (amended by this ADR),
  [ADR-0009](ADR-0009-rename-and-deprecate.md)

## Context

ADR-0013 was built (server, client, web UI) and its tests passed. An independent review, by a
reviewer with no part in the design or the code, then found defects the tests did not reach and
questioned two rules. Its findings, verified where marked:

| Id | Finding |
|---|---|
| C1 | A manager's `force_shrink` (or an import with "Replace") deletes every style, its own and people's: the style rule dropped existing entries and the replace stored the rest whole (verified). |
| M1 | Redefining a search index under an existing name is not seen as a change (the diff compares names only), so anyone with `maintain` bypasses the manager (verified); nor is it in the change feed. |
| M2 | Who may set the manager is checked against this replica's in-memory registry, outside the schema-write lock; and an unchanged value still needs `owner`, so a manager that is not an owner is refused at every start. |
| M3 | The drift report is wrong for the manager's own dry run (its styles reported as "not declared"), calls items "kept" that a replace removes, and leaves out most parts (rules, indexes, columns, hierarchy, settings, `meta` keys, explicit removals). |
| M4 | "The manager never touches an existing style" leaves the program no way to correct or remove a style it shipped; the precedents cited do not support a rule the server imposes on its own. |
| M5 | `GraphosPool` changed for every user: a dry run before every registration, and warnings for every part a pool does not declare. |
| M6 | Unregistering and restoring a tenant drops its manager: a silent way round the rule. |
| m1 | `ensure_tenant`'s check can go stale between the dry run and the registration. |
| m2 | An older (or rolled-back) server ignores `schema_manager` and the client cannot tell. |
| m3 | An import of more than 5,000 pairs sets the manager in its first call; the later calls are then refused. |
| m4 | Setting the manager, forgetting removals and the change record run in separate transactions. |
| m5 | A manager is not checked to exist or be enabled; with authentication off, a manager freezes everyone. |
| m6 | A program signed in through the identity provider can never be a manager. |
| m7 | The 409 is not in the OpenAPI document; `drift` is untyped. |
| m8 | A replace registration's removals are not recorded. |
| m9 | ADR-0013's text disagrees with the code in places. |
| n1–n5 | `schema_manager_since` unused; duplicated constants; an unused argument; three encodings of a pair and two vocabularies; an `assert` in an exception handler. |

The review also proposed a simpler alternative: a per-tenant flag "structure writes need `owner`".

## Decision

Keep the schema manager (ADR-0013), with these changes:

1. **Styles merge as the registration says** (M4). A registration may send
   `display_merge: "entries" | "missing_only"`: `entries` is the merge every registration has
   (an entry declared replaces the stored one, `null` removes it), `missing_only` adds only the
   entries the tenant does not have. The manager's registrations on a managed tenant default to
   `missing_only`, anyone else's to `entries`. A program that ships styles it wants to correct
   sends `entries`; people's styles are then its to overwrite, by its own choice. A replace
   (`force_shrink`) never loses a stored style under `missing_only` (C1).
2. **A change is everything the change feed records, and more of it is recorded** (M1): a search
   index redefined under the same name is a change, in the diff, the change feed and the check.
3. **Who may set the manager is checked under the lock against the stored tenant** (M2); a
   registration whose `schema_manager` equals the stored one is no change and needs no right.
4. **The drift report covers every part the diff knows, from what the registration declares**
   (M3): `not_declared`, `overwrites` and `recreates` as before, plus `removes` (what a replace
   would take away) and `settings` (`strict_endpoints`, property mode, community algorithm the
   body would change). It is computed before the manager's style rule, from the merged result.
   Its three lists name items the same way: `vertex_labels`, `edge_labels`, `edge_endpoints`
   (pairs as `[type, src, dst]`), `search_indexes`, and the by-entry parts (n4).
5. **The pool keeps its behaviour** (M5): drift checking is a `GraphosPool` option, `"ignore"`
   by default. In the client, `not_declared` is logged at INFO (most programs declare less than
   the tenant has); `overwrites`, `recreates`, `removes` and `settings` at WARNING.
6. **The manager survives unregister and restore** (M6), kept with the registration it belongs
   to.
7. **The community algorithm is not structure**: an operational setting; anyone with `maintain`
   may change it on a managed tenant.
8. **Who can be a manager** (m5, m6): `user:<name>` of an existing, enabled graphos user, or
   `oidc-sub:<sub>` of a caller signed in through the identity provider; `"self"` names the
   caller, whichever it is. With authentication off, naming a manager is refused (422).

Considered and not taken: **"structure writes need `owner`"**. It reuses grants and works for
groups, but `owner` is also what people need to manage a tenant's access; whoever manages access
could then change the structure, which is what the manager exists to prevent. A tenant whose
owners are only programs may get the same effect by granting people `maintain`.

## Tasks

| Task | Finding | What | Done when |
|---|---|---|---|
| T1 | C1, M4 | `display_merge` on the registration; manager default `missing_only`; replace keeps stored styles under `missing_only` | DB test: a manager's `force_shrink` keeps people's and its own styles; `entries` lets it correct and remove one |
| T2 | M1 | Search-index redefinitions in `schema_changes.diff` (`changed.search_indexes`), so the check, the feed and the report see them | Unit test of the diff; DB test: a non-manager's redefinition is refused |
| T3 | M2 | Manager right checked in `_register` under the lock against the stored spec; unchanged value is no change (no right, no write, no notify) | DB test: a non-owner manager registers with its own name at every start; a `maintain` user cannot set it |
| T4 | M3, n4 | Drift report from the declared spec and the merged diff: `not_declared`, `overwrites`, `recreates`, `removes`, `settings`, one vocabulary | Unit tests for each part; DB test: a manager's dry run reports no false `not_declared` styles; a replace reports `removes` |
| T5 | M5 | `GraphosPool(..., on_drift="ignore")`; INFO for `not_declared`, WARNING for the rest | Client tests |
| T6 | M6 | `schema_manager` kept in `graphos_meta.unregistered_tenants` and restored | DB test: unregister + restore keeps the manager |
| T7 | — | Community algorithm open to `maintain` on a managed tenant | DB test |
| T8 | m5, m6 | Manager subjects: existing, enabled `user:<name>`, `oidc-sub:<sub>`, `"self"`; 422 with authentication off | DB tests for each |
| T9 | m1 | The dry run answers the `ETag` it judged; `ensure_tenant` registers with it as `If-Match` | Client test; DB test of the header |
| T10 | m2 | `RegisterResponse.schema_manager`; the client checks it after naming a manager and raises when the server ignored it | Client test |
| T11 | m3 | `import_schema` sends `schema_manager` with its last call | Client test |
| T12 | m4 | Manager change, forgotten removals and the change record in one transaction | Code review; DB tests still pass |
| T13 | m7 | 409 `schema_managed` declared on the four routes; `drift` a typed model | OpenAPI test |
| T14 | m8 | A replace registration records the labels, edge types and pairs it removes | DB test: replace, then re-declare, reports `recreates` |
| T15 | n1–n5 | `schema_manager_since` served with the manager; one source for shared constants; no unused argument; typed exception handler | Lint, types, tests |
| T16 | m9 | ADR-0013's text in line with the code; "amended by ADR-0014" | Read through |
| T17 | — | Docs (usage, concept, features), CHANGELOG, client README | Read through |
| T18 | — | Independent review again on the result | Its findings reported unedited |

### After the second review (T18)

The second independent review found two Major defects and seven Minor ones (R1–R10); each became a
task, done in the same change.

| Task | Finding | Decision | Done when |
|---|---|---|---|
| T19 | R1 Major | A caller's manager subject is its own grant subject: single sign-on principals already carry `oidc-sub:<sub>`, never prefixed again | A unit test through `AccessStore.principal_for`; the DB test's principal built as production builds it |
| T20 | R2 Major | A replace under `missing_only` keeps stored styles only for the labels and edge types it keeps | Unit and DB tests: a replace that removes a styled label previews and registers, and stores no style for it |
| T21 | R3 | `ensure_tenant` judges the new version once more when its registration answers 412 (replicas starting together), then gives up | Client tests: one move succeeds, two raise 412 |
| T22 | R4 | The dry run serves the version read with the spec it judged | Code read; the dry-run DB test |
| T23 | R5 | A registration that restores is judged on the tenant it restores before restoring it (a refused one restores nothing); unregister and restore keep `schema_manager_since` | DB test: a refused restoring registration, preview and real, leaves it unregistered |
| T24 | R6 | `GET /v1/me` gains `subject` (additive); the UI compares the manager with it, and falls back for an older server | UI and server unit tests |
| T25 | R7, R8 | Documented, not changed: a manager whose user is deleted, or authentication turned off later, keeps the structure until an owner releases it; during a rolling upgrade an older replica serves another `ETag` for a managed tenant and its unregister drops the manager | usage.md |
| T26 | R9, R10 | Tests for the 409 and `DriftReport` in OpenAPI and for removing a style with `entries`; a name a rename brought back is not reported under `recreates`. Kept as they are: `removed_by` names the caller as change records do; `missing_only` does not report styles people changed (they are theirs); a tenant created by someone else between `ensure_tenant`'s two calls is registered without a check, as `register_tenant` always was | Tests; this table |

## Consequences

- The program decides how its styles merge; the safe default protects people's styles, and a
  replace can no longer lose them.
- The drift report becomes a complete account of what a registration would do, so `ensure_tenant`
  can rely on it, and a pool keeps its old behaviour unless asked.
- More single-sign-on deployments can use a manager; graphos users named as managers must exist.
- The registration answer and the dry run gain fields; no route is added.

## Acceptance

Every task's "Done when" holds; the full DB suite, unit, client and UI suites, ruff and mypy pass;
the second independent review raises nothing Critical or Major once its findings are done
(T19–T26), which a third, focused review of those fixes checks.

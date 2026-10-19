# ADR-0009: Renaming and deprecating labels, edge types and properties

- **Status:** Accepted (2026-10-19)
- **Scope:**
  - the tenant's schema: a `deprecated` section in the registration spec, and a rename endpoint;
  - AGE's catalogue and graphos's tables, indexes and `graphos_meta` rows for a label or edge
    type rename;
  - a background job that rewrites one property on every row of a label or edge type;
  - the change feed, the schema summary, the client and the web UI's Schema page.
- **Related:** concept §2.2 (registration), §2.6 (jobs); ADR-0001 (one transaction per scope);
  `schema_breaking.py` (what a dry run calls breaking)

## Context

A tenant's schema only grows. Registration (`POST /v1/tenants`) adds labels, edge types, property
definitions and indexes; `?force_shrink=true` replaces the stored spec but keeps every table and
row; `DELETE …/edge_endpoints` removes one declared label pair. Nothing renames anything, and
nothing marks a name as on its way out.

So a schema mistake is permanent, or costs a rebuild:
- **A label or edge type named wrong** (`Servce`, `DEPENDS`, a name the domain later changed) can
  only be fixed by registering the new name, copying every row and edge through the API, and
  leaving the old table empty. The graph ids change, so every edge has to be written again.
- **A property named wrong** has to be written again on every row by the consumer, a whole-label
  read and write through the API, while readers see some rows with each name.
- **Retiring a name** has no signal: consumers learn it is gone when their writes start failing.

A rename touches far more than the label table. A name lives in AGE's catalogue (`ag_label`), the
table, its sequence and the `id` column's default (which names the label), about ten indexes
graphos names after the label (`bootstrap.py`), and in `graphos_meta`: the stored spec (vertex and
edge lists, label hierarchy, property definitions, display, declared, search and resolver
indexes, resolve rules), `edge_endpoints`, `node_properties`, and the community map. Saved queries
name labels in Cypher text. The change feed's past records name the old spelling.

AGE has no function to rename a label. Its catalogue is a plain table, and the table, sequence and
default can be renamed with ordinary DDL (see Evidence).

## Decision

Three operations, each with a dry run. Deprecation changes no data; a label or edge type rename is
one short transaction; a property rename is a background job.

### 1. Deprecation

The registration spec gains `deprecated`, per vertex label and edge type, merged by entry as
`display` is (an entry given again replaces the stored one, `null` removes it); a registration,
so `maintain` level:

```json
"deprecated": {
  "vertex": {"Servce":  {"deprecated": true, "replaced_by": "Service", "note": "typo"},
             "Service": {"properties": {"owner_team": {"replaced_by": "team"}}}},
  "edge":   {"DEPENDS": {"deprecated": true, "replaced_by": "DEPENDS_ON"}}
}
```

- **Writes** naming a deprecated label, edge type or property succeed and carry a warning in
  `property_warnings` (rule `deprecated`), **whatever the mode**. A refusal in `strict` mode was
  left: a patch is checked on the row it leaves, so it would refuse every patch of an old row that
  still carries the property, not only new data. The warning, the schema and its summary steer new
  writes instead.
- **The schema summary** (and MCP's `get_schema`) lists deprecated names with what replaces them;
  `GET /v1/{tenant}/schema` serves them; the web UI's Schema page shows them marked.
- **Removal** is explicit: `DELETE /v1/tenants/{name}/labels/{label}` (`owner`) drops a deprecated
  label or edge type **only when it has no rows**, with its indexes and `graphos_meta` rows, in one
  transaction; refused while a label inherits from it, a resolve rule requires it as a destination,
  or a deprecation names it as the replacement. A deprecated property is removed from the
  definitions by declaring the entry without it; its values stay on the rows until a consumer
  writes them away (no "unset everywhere" job in this ADR).

### 2. Renaming a label or an edge type, in place

`POST /v1/tenants/{name}/schema:rename` with `{"kind": "label" | "edge_type", "from": …, "to": …}`,
`owner` level (it changes what every consumer of the tenant sees). `?dry_run=true` answers what it
would change and writes nothing.

**Checks first** (answers 400 or 409, nothing changed):
- `to` is a valid label or type name, not reserved, and not used by any label or edge type of the
  tenant (AGE's names are unique across both kinds);
- no job is queued or running on the tenant, and no reconcile holds the edge type's lock;
- the tenant's advisory lock is free within `lock_timeout` 5 s (409 when busy), as for tenant
  deletion (`lifecycle.drop_tenant_data`).

**One transaction**, holding the tenant's advisory lock:
- **AGE:** `ag_label.name` and `seq_name`; `ALTER TABLE … RENAME`; `ALTER SEQUENCE … RENAME`; the
  `id` column's default reset to name the new label. The table keeps its OID, so inheritance
  (label hierarchy), triggers (AGE 1.8's cache trigger), constraints and the rows' graph ids do not
  change; no row is written.
- **Indexes** graphos named after the label (`{label}_properties_gin`, `_id_unique`,
  `_community_id`, `_name_trgm`, the edge indexes, declared, search and resolver indexes):
  `ALTER INDEX … RENAME`, so bootstrap does not build a second set under the new name.
- **`graphos_meta`:** the spec (vertex or edge list, hierarchy, property definitions, display,
  declared/search/resolver indexes, resolve rules: the edge-type key and `require_dst_label`),
  `edge_endpoints`, `node_properties`, `community_map.labels` and `community_links.type`.
- **The spec's `renames`** history gains `{kind, from, to, at, by}`.
- **One `schema` change record** with `detail.renamed: {kind, from, to}`. Past records keep the
  old spelling: the feed is a history, and the rename record says where the name went.
- **`NOTIFY graphos_tenants`** in the same transaction, so every replica reloads at once (the
  30-second registry sync covers a missed notification).

**After the rename:**
- **Writes naming the old name** answer **409** `"<from>" was renamed to "<to>" on <date>`, read
  from `renames`, instead of a missing-relation error. This also covers a replica that has not
  reloaded yet.
- **Reads naming the old name**: the API's own routes answer as for any unknown label; **Cypher**
  (the console, saved queries, MCP `run_cypher`) does not fail but returns nothing, which is AGE's
  behaviour for an unknown label (see Evidence). So the rename **reports the saved queries whose
  text names the old name** (the console's tokeniser finds `:Label` and `[:TYPE]`), in the dry run
  and the answer. It does not rewrite them: a query's text is its owner's, and a textual rewrite
  could change other identifiers.
- **Registering the old name again** is refused the same way (409), so an import of a schema or
  snapshot file made before the rename says where the name went instead of creating it again.
  Exports carry the new names.

### 3. Renaming a property

`POST /v1/tenants/{name}/schema:rename` with `{"kind": "property", "on": <vertex label or edge type>,
"from": …, "to": …}`, `owner` level, runs as a **background job** (`rename_property`, 202 with
`Location: /v1/jobs/{id}`). On a vertex label it covers the labels below it in the hierarchy. One
property rename per tenant at a time (a partial unique index on `graphos_meta.jobs`). Three
phases:

1. **Start** (one transaction under the tenant's lock): the job is queued and the spec's
   `renames` records the rename as `running`. Writes naming `from` are stored as written, with a
   warning (rule `renamed`); mapping them to `to` was left, as a second write path for a few
   minutes' gain.
2. **Backfill**: rows are rewritten in batches of 25,000, each its own transaction: `to` takes
   `from`'s value and `from` is removed. Each rewritten row gets a new `graphos_rev` (its ETag
   changes, as for any write); each batch writes one summary change record (`op: patch`,
   `detail.renamed_property`), not one per row. Passes repeat until a whole pass finds no row with
   `from`, so writes made meanwhile are rewritten too. The job reports **progress** (rows done of
   rows to do), a new column on `graphos_meta.jobs`, and can be cancelled between batches; a
   cancelled or failed rename is marked `failed` and resumes when asked again.
3. **Finish** (one transaction): the definitions, the deprecation, display captions, property and
   search indexes and the property catalogue move to `to`, the rename is marked `done`, and from
   then on a write naming `from` is refused (rule `renamed`, in any mode). The indexes are then
   built under the new name and the old ones dropped.

**Refused** (400, before anything changes):
- `from` is `id`, a reserved name (`graphos_*`, `community_id`, …), or `to` is;
- a row already has both `from` and `to` (the dry run counts them; the owner resolves them first);
- a declared **generated column** computes from `from`: Postgres would rewrite the whole table to
  change its expression, so the owner drops the generated column first and declares it again;
- a **resolve rule** names the property, or a **search index** over other labels uses it: either
  would change for labels the rename does not cover.

Renaming `name` on a vertex label is allowed, with a warning in the answer (`warnings`): search
without an index matches `name` by default, through the trigram index graphos builds on it.

### 4. Who sees what

- **Client:** `rename_label`, `rename_edge_type`, `rename_property` (its answer carries the job's
  id: `wait_for_job(result.job_id)`), `drop_label`, and `deprecated` and `renames` in the models.
- **Web UI, Schema page:** rename and deprecate actions on a label, type or property, with the dry
  run's report (what changes, rows to rewrite, saved queries to check) before confirming.
- **MCP:** no new tool; `get_schema` marks deprecated names and lists recent renames, so an agent
  does not write the old name into a query.

## Decision options

- **Rename in place (taken).** No row is written; graph ids, ETags, edges and the hierarchy stay.
  Costs: graphos writes AGE's catalogue table directly, with no supported AGE function; the table is
  locked (`ACCESS EXCLUSIVE`) for the rename, which is a catalogue change and takes milliseconds.
  The upgrade risk is checked by a test on every AGE version graphos supports.
- **Copy into a new label.** Uses only AGE's public functions, but every row is written again, the
  label number inside every graph id changes, so every incident edge's `start_id`/`end_id` must be
  rewritten too, every ETag breaks, and a large label takes a long job. Left.
- **An alias layer** (a logical name in the spec mapped to the physical table). Cheap to store,
  but every read path that returns a label (from `pg_class` or `ag_label`, about a dozen places)
  must map names back, and the Cypher console and saved queries would still see the physical name.
  Two names for one thing, forever. Left.
- **A property rename in one transaction.** Simple, but holds row locks on a whole label for as
  long as the rewrite takes, against a 60 s statement timeout; the phased job keeps each
  transaction short and the data writable. Left.
- **Migration files** (an ordered list of schema changes applied at startup, as SQL migration tools
  do). graphos's schema is data in `graphos_meta`, registered over HTTP by consumers that may run
  several versions at once; a file of migrations would need its own ordering and history. The
  `renames` history plus the change feed's schema records give the same audit trail. Left.

## Evidence

**AGE 1.8 on PostgreSQL 18, a throwaway database:** a vertex label `Old` with two rows and an edge
was renamed to `New` in one transaction: `UPDATE ag_label SET name, seq_name`, `ALTER TABLE …
RENAME`, `ALTER SEQUENCE … RENAME`, and the `id` default reset (before:
`_graphid(_label_id('spk','Old'), nextval('spk."Old_id_seq"'))`). Then, on three connections (the
one that renamed, one that had queried `Old` just before, and a new one):
- `MATCH (n:New)` returned every row; `MATCH (x:New)-[:LINKS]->(y:New)` followed the edge;
- `CREATE (:New …)` and a plain SQL `INSERT` (using the reset default) worked;
- `MATCH (n:Old)` returned **no rows, without an error**;
- every row kept its graph id, whose high bits still name the label's id.

So AGE's per-connection label lookups saw the catalogue change at once, and no row needed writing.
The acceptance tests since run the same on an edge type, on a label with a hierarchy child and on
another pooled connection.

## Consequences

- **Better:** a schema mistake is fixed in milliseconds (labels, types) or by a resumable job
  (properties), without a rebuild or new graph ids; retiring a name has a visible, gradual path.
- **Worse:**
  - graphos depends on AGE's catalogue layout (`ag_label`, the `id` default's form). Each AGE
    upgrade must pass the rename tests before graphos supports it.
  - Cypher that names an old label silently returns nothing; the saved-query report and `get_schema`
    reduce the risk, they do not remove it for queries kept outside graphos.
  - During a property backfill, readers see some rows with each name, and writers may still write
    the old one (with a warning) until it ends. Consumers that filter on the property should wait
    for the job.
- **Must change with it:** concept §2.2 and the endpoint table, usage (a "Changing a schema"
  section), features.md, the client and its models, the web UI's Schema page, the schema document
  (version 3 when it carries `deprecated`; the rename history stays with the tenant, not in the
  file), the CHANGELOG. `schema_breaking.py` is unchanged: renames do not go through
  registration.

## Open questions

Answered on acceptance:
- An import of an older file naming a renamed name is **refused** with where the name went; no
  rename map. The owner edits the file, which is plain JSON.
- `renames` is **kept**: a short list per tenant, and the only record of where a name went.
- **No read mapping**: filters naming the old property name match nothing once rows are
  rewritten; the job's progress and the schema summary say when.
- Deprecation is **`maintain`** (it is a registration and changes no data); renames and drops are
  `owner`.

## Acceptance

Tests on PostgreSQL 18 / AGE 1.8:
- **Label and edge-type rename:** rows, graph ids and edges unchanged; inheritance children still
  inherit; every graphos index renamed and none duplicated after a restart's bootstrap; the spec,
  `edge_endpoints`, `node_properties`, the community map and resolve rules use the new name;
  `edges:resolve` and reconcile work under it; a write to the old name answers 409 naming the new
  one, on this replica and on a second one before it reloads; the console pool and a new connection
  see the new name; one `schema` change record; the dry run writes nothing.
- **Refusals:** a taken or reserved name, a running job, a busy tenant (409 within 5 s).
- **Property rename:** every row has `to` and not `from`; ETags change; one summary record per
  batch; progress reported; cancel then resume finishes; a row with both names, `id`, a reserved
  name and a generated column are refused before any change; indexes rebuilt on `to`.
- **Deprecation:** warnings on writes in every mode, shown in the summary and `get_schema`; an
  empty deprecated label is dropped, a non-empty one is refused.
- **Saved queries** naming the old label or type are reported by the dry run and the rename.
- **Docs** updated as listed under Consequences.

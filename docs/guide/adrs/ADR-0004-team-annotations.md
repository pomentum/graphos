# ADR-0004: Team annotations: saved views, notes and flags

- **Status:** Proposed (2026-10-16)
- **Scope:**
  - three new tables in `graphos_meta`, and their API under `/v1/{tenant}/`;
  - a change-feed kind;
  - the web UI's Explore canvas, inspector and Browse.
- **Related:**
  - the reserved-names rule (`reserved.py`);
  - ADR-0001 (change records).

## Context

**Today a canvas lives only in the browser tab.** The nodes on it, their positions, layout and
styling are lost on reload, and cannot be shared. Saving and sharing a view is the main thing a
team does with a graph tool. Users in regulated settings also need their findings kept and
attributable.

**Other tools:**
- Investigation tools keep saved canvases and analyses with notes, and a per-node flag, but in
  the browser or per user.
- graphos already keeps such things on the server, per tenant:
  - saved Cypher queries (`graphos_meta.saved_queries`);
  - label styles (the tenant spec's `display`);
  - user preferences.

**A first design put flags in a reserved property (`graphos_flag`).** That conflicts with
graphos's rule that every write endpoint refuses `graphos_*` names (`reserved.py`): a user could
not set a flag through the API without an exception to that rule. Flags in node properties would
also:
- bump the node's `graphos_rev`;
- show in Cypher and exports as if they were data.

## Decision

**Annotations are kept beside the graph, not in it.** Three kinds, each per tenant, in
`graphos_meta`. They are written only through their own endpoints, audited, and recorded in the
change feed. They never touch label tables, node properties or `graphos_rev`.

### 1. Saved views

**What a view holds** (`graphos_meta.views`):
- `tenant`, `id` (uuid), `name` (unique per tenant), `description`;
- `definition` (jsonb), `rev`, `created_by`, `created_at`, `updated_by`, `updated_at`.

**`definition`:**
- **References, not data:** the nodes as `(label, id)`, with their positions.
- **The canvas state:** the camera, the layout name, and the colour and size choices (or "the
  tenant's styles").
- **The rest:** the active filters, and the pinned and hidden nodes.

**Opening a view:**
- fetches the nodes as they are now;
- reports the ones that no longer exist;
- never shows stale property values.

**Limits:**
- `GRAPHOS_VIEW_MAX_NODES`, default 5,000;
- a definition of at most 2 MB.

**API:**
- `GET /views` (names, authors, times);
- `POST /views`;
- `GET /views/{id}`;
- `PUT /views/{id}` with `If-Match` (the existing ETag pattern; a 412 says someone else saved
  first);
- `DELETE /views/{id}`.

### 2. Notes

**What a note holds** (`graphos_meta.notes`):
- `tenant`, `id`, `target`, `body`, `mentions`, `rev`;
- `created_by`, `created_at`, `updated_by`, `updated_at`.

**What a note can be about:** the tenant, a saved view, a node or an edge. `target` is one of:
- `{"kind": "tenant"}`;
- `{"kind": "view", "id"}`;
- `{"kind": "node", "label", "id"}`;
- `{"kind": "edge", "type", "src", "dst"}`.

**The format is Markdown, recommended** (open question 1):
- **Stored as text:** portable, diffable, readable in an export.
- **Mentions** are links in the body: `[@billing](graphos:node/Service/billing)`. The server
  extracts them into `mentions` (jsonb, GIN-indexed), so "notes mentioning this node" is one query.
- **Rendering:**
  - the UI renders without raw HTML (react-markdown, MIT, HTML off);
  - links are limited to `http`, `https` and `graphos:`.
- **The editor:** a textarea with `@` autocomplete over node search; Milkdown (MIT) if a
  WYSIWYG editor is wanted later.
- **Rich text (TipTap, MIT) was the alternative:** it stores JSON, needs HTML sanitising on every
  render, and an export would need a renderer to be readable.

**Size:** at most 64 KiB of body.

**API:**
- `GET /notes?target=…` or `?mentions=label/id`, cursor-paged;
- `POST /notes`;
- `PATCH /notes/{id}` with `If-Match`;
- `DELETE /notes/{id}`.

### 3. Flags

**What a flag holds** (`graphos_meta.node_flags`):
- `tenant`, `label`, `node_id`, `color`, `note` (≤ 280 characters), `set_by`, `set_at`;
- primary key `(tenant, label, node_id)`: one flag per node.

**Colours:** `red`, `orange`, `yellow`, `green`, `blue`. Meanings are the team's own; the tenant
spec may name them, for example `display.flags: {"red": "blocking"}`.

**API:**
- `GET /flags?label=&color=`, cursor-paged;
- `PUT /flags/{label}/{id}`;
- `DELETE /flags/{label}/{id}`.

**Reads:**
- `nodes:scan` and `search` take `flag=<color>` (a join on the flags table);
- node reads take `include=flag`.

**In the UI:**
- a flag is drawn on the canvas node, and shown in the inspector and in Browse;
- Browse can filter by flag.

### Permissions

| Action | Access |
|---|---|
| list or open views, read notes, read flags | `read` |
| save a view, update any view, write a note, set or clear a flag | `write` |
| edit or delete a note | its author, or `maintain` (delete only; edits stay the author's) |
| delete a view | its author, or `maintain` |

**Attribution:**
- A note's body is only ever changed by its author, so a finding stays attributable.
- Views are shared work: any writer may update one. `updated_by` and the change feed say who did.

### Change feed and audit

**The change feed:**
- `changes.Kind` gains `view`, `note` and `flag`, with the existing ops `upsert` and `delete`;
- records carry ids, never note bodies;
- a consumer that filters on `node` and `edge` sees no difference.

**The audit:** every write is audited as today, since these are `POST`, `PUT`, `PATCH` and
`DELETE` routes.

### When the graph changes

| Event | Effect |
|---|---|
| A node is deleted (any path, including by property and by reconciliation) | **its flag is deleted in the same transaction**; notes on it are kept and shown as "on a deleted node", because a finding must outlive its subject |
| A view references deleted nodes | the view opens without them and lists them; the next save drops them |
| `DELETE /tenants/{name}?drop_data=true` | the tenant's annotations are deleted |
| Snapshot export / import, dataset export | annotations are an optional section (`annotations: true`); import keeps the authors and times as recorded |

## Decision options

| Option | For | Against |
|---|---|---|
| **Annotations beside the graph, in `graphos_meta` (chosen)** | the reserved-names rule unchanged; no revision bumps; Cypher and exports keep showing only data; own permissions and audit | a join to filter nodes by flag; three small APIs |
| Flags as a reserved node property (`graphos_flag`) | Cypher and filters see it for free | breaks "every write refuses `graphos_*`"; bumps `graphos_rev`; mixes team opinion into data |
| Views and notes in the browser (as investigation tools keep them) | no server work | not shared, lost with the browser; not attributable |
| Notes as rich text (TipTap JSON) | WYSIWYG editing | sanitising on every render; unreadable in an export without a renderer |
| Notes as nodes of a reserved label in the tenant's graph | mentions become real edges | annotations would show in every graph read, count, algorithm and export; needs reserved labels |

## Consequences

- **New tables:** three, created by `ensure_internal_schema` like the others. There is no tenant
  data migration.
- **Node-delete paths:** `writes.delete_node`, `delete_nodes` and ADR-0001's reconciliation
  (node scope does not delete nodes; unaffected) gain one statement for flags. The test that
  checks every write logs a change covers the new writes.
- **The UI gains:**
  - "Save view" and "Open view" on Explore;
  - a notes panel on the inspector and on views, and the flag control;
  - Browse's flag filter.
  - The canvas state needs a serialisable form, which the UI's workspace already almost has.
- **The Python client** gains `views`, `notes` and `flags` methods and models.
- **Documentation:** `features.md` (a new section), `concept.md` (annotations are not data, and what
  happens on delete), and the API reference.
- **Mentions** make "what did the team say about this node" one call. A later full-text search
  over notes can use the same table.

## Acceptance

Tests on PostgreSQL 16 and 18:
- **Views:**
  - save, open and update with `If-Match` (412 on a stale ETag);
  - a view with a deleted node opens and lists it as missing;
  - the size limits are enforced.
- **Notes:**
  - an author edits; another writer cannot edit; `maintain` deletes;
  - mentions are extracted, and `?mentions=` finds the note;
  - raw HTML in a body is not rendered by the UI (component test).
- **Flags:**
  - set, change and clear;
  - `nodes:scan?flag=red` returns only flagged nodes;
  - deleting the node deletes its flag in the same transaction;
  - a property write naming `graphos_flag` is still refused.
- **The feed:** every annotation write records a change of the new kinds, with no body.
- **Snapshots:** an export and import with `annotations: true` round-trips views, notes and flags.

## Open questions

1. Notes in Markdown (recommended) or rich text?
2. May any writer update a shared view (proposed), or only its author, with "save as" for others?
3. Flag meanings: free per team (proposed), or a fixed vocabulary?

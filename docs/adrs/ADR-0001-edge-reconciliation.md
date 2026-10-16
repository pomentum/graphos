# ADR-0001: Edge reconciliation by difference

- **Status:** Accepted (2026-10-16)
- **Scope:** the data plane: a new write endpoint, a mode for `edges:resolve`, change records
- **Related:** concept §8 limitation 4 (`edges:resolve` only inserts); an ADR on edge history (to
  write, see "Edge history" below)

## Context

graphos writes edges by upsert only:
- `POST /graph/edges:bulk` (`writes.upsert_edges`) is idempotent on `(start_id, end_id)` per edge
  type, and merges properties into an existing edge;
- `edges:resolve` (`writes.insert_resolved_edges`) inserts the edges a rule derives, with
  `ON CONFLICT DO NOTHING`.

Neither removes an edge that the source no longer has. A consumer that reloads its data (a re-scan,
a new import run) keeps every edge it once wrote. Today it has two ways out:
- **Delete and reload:** delete the run's nodes (`DELETE /graph/nodes?property_key=&property_value=`),
  load again and resolve again. `tests/test_run_replace_db.py` proves this equals a fresh load. The
  cost is a full rebuild: 88 s for 1M edges, and readers see the graph half-empty meanwhile.
- **Work out the difference itself:** read the old edges, compare, delete each stale edge with
  `DELETE /graph/edges/{type}`. Many calls, no transaction, and every consumer writes the same code.

Rule-derived edges have the same problem, inside graphos: when a node's property changes, the edges
its old value derived stay (limitation 4).

Graph tools that reload a source keep the difference server-side: one such tool compares the new
set of a node's edges with the stored set, adds the new ones, and ends the missing ones.

## Decision

Add **reconciliation**: the caller sends the complete set of edges for a scope, and graphos makes
the stored set equal to it in one transaction:
- it adds the missing edges;
- it updates the kept edges whose properties change;
- it removes the edges that are not in the set.

### The scopes

| Scope | What the set replaces | Use |
|---|---|---|
| `node` | the edges of one type at one node, in one direction, whose other end has a given label | a node's source record changed |
| `type` | every edge of one type between two labels | a whole relationship was re-extracted |

**Both scopes need the other end's label:**
- an edge type can join several label pairs, and the targets are external ids, found in that label's
  table;
- edges of the same type to other labels are not touched.

### The API

`POST /v1/{tenant}/graph/edges:reconcile`, which takes `Write` access.

**A `node` set, as JSON:**

```json
{
  "scope": "node",
  "node": {"label": "Service", "id": "billing"},
  "type": "DEPENDS_ON",
  "direction": "out",
  "other_label": "Service",
  "edges": [{"id": "ledger", "props": {"weight": 3}}, {"id": "auth"}],
  "props": "merge"
}
```

**A `type` set**, as JSON (up to 10,000 edges, each `{"src_id", "dst_id", "props"}`) or, for a
larger set, as NDJSON:
- the first line is the header `{"scope": "type", "src_label", "type", "dst_label", "props"}`;
- each following line is one edge `{"src_id", "dst_id", "props"}`;
- the lines are COPYed into a staging table, so the set can be as large as the type.

**The parameters:**
- `props`:
  - `merge` (the default, as upsert does) merges the given properties into a kept edge;
  - `replace` makes them the kept edge's whole properties;
  - a kept edge sent without `props` keeps its properties under either mode.
- `?dry_run=true` computes the difference, answers it, and rolls back.

**The answer:**
- the counts `added`, `changed`, `kept`, `removed`, `skipped`;
- for a `skipped` target (its node does not exist), the first 100 targets are listed. A skipped
  target is never created, as with upsert.

**The reserved names** are refused in `props`, as on every write.

### How it runs

One transaction per request:
1. Take an advisory lock on `(tenant, type)`. Two reconciliations of the same type queue, and other
   types run in parallel.
2. Stage the set (COPY), and resolve the external ids against the label tables (the same index
   probes `upsert_edges` uses).
3. Delete the stored edges in the scope that are not in the staged set:

   ```sql
   DELETE … WHERE start_id = $n AND <other end has the label> AND end_id <> ALL(staged)
   ```

   For the `type` scope, this is an anti-join against the staged set.
4. Insert the staged edges that are new, and update the kept edges whose properties differ:

   ```sql
   INSERT … ON CONFLICT (start_id, end_id) DO UPDATE … WHERE properties IS DISTINCT FROM …
   ```

**Revisions:** an edge that is kept unchanged is not written, so its `graphos_rev` does not move.
"The revision counts writes" still holds, because nothing is written. This matters for the `type`
scope, where most edges are kept.

**Concurrency:** a concurrent plain upsert of the same type is not blocked, and the last writer
wins. Reconciliation means "the set is exactly this", so an edge upserted before the delete
statement starts is removed if it is not in the set. The change records show both writes.

### Change records

Two existing ops, in the same transaction, so feed consumers need no new op:
- `upsert` for the added and changed edges;
- `delete` for the removed edges.

**Fields:**
- Both carry `detail: {"reconcile": "<scope>"}`.
- Edge ids are listed as `[src_id, dst_id]` pairs up to 10,000.
- Above that, `ids` is left out and `count` stays exact, as `resolve` records do.

### Rule-derived edges: `edges:resolve` by difference

**Marking:**
- An edge written by `edges:resolve` carries the reserved property `graphos_derived: true`.
- Consumers cannot write it: it has the `graphos_` prefix.

**The new mode:** `edges:resolve` takes `mode`:
- `insert`: today's behaviour, and the default for one release;
- `reconcile`: computes the union of every rule of the edge type into a staging table, then applies
  the same difference to the marked edges of that type.
- An edge the consumer wrote itself is never removed by a rule.

**Edges derived before this release** carry no mark:
- A `reconcile` run leaves unmarked edges alone: it cannot tell an edge a rule derived before the
  mark existed from one a consumer wrote.
- `adopt_unmarked: true` takes every unmarked edge of the type between the rules' label pairs as
  rule-owned: those the rules derive are marked, the others deleted.
- `dry_run` shows the effect before adopting.

This closes limitation 4.

### Edge history: not in this ADR

Keeping removed edges with an `ended_at` time, as some graph tools do, is left out on purpose:
- **What stays open:** `removed` is not a parameter in this version. "Remove" means delete. A later
  ADR can add `"removed": "end"` without changing the request shape.
- **Why it waits:** keeping ended edges in the label tables means every read must skip them. graphos
  can add that filter to its own reads, but not to the Cypher console or to a plain AGE copy of the
  data. Both would show ended edges as current.
- **The likely design:** move removed edges to a per-tenant history table in the same transaction.
  The label tables then hold only current edges, and every read path stays correct untouched.
- **Measured since (2026-10-16):** an embedded graph library runs this design (live tables
  current-only, an edge history table with `reason` and `superseded_by`, as-of reads as
  `live UNION ALL history`). On a 1M-edge graph with graphos's table shape:
  - removing 50,548 edges and moving them to history in the same statement
    (`WITH d AS (DELETE … RETURNING *) INSERT INTO history …`) took 3.33 s, against 3.63 s for the
    plain delete: archiving costs nothing measurable; 8.3 MB of history;
  - as-of one hop on a hub: 1.4–1.7 ms (current 0.7–2.8 ms);
  - as-of two hops: 3–14 ms when the union is written per hop (a lateral join), 330 ms–4.9 s when it
    is one CTE over the union. As-of reads must be compiled per hop.
  - It needs a `graphos_since` timestamp on edge tables (a constant default, so the `ADD COLUMN` is
    instant).
- **What this ADR keeps for it:** the reconcile's removal step is the one place that would archive,
  so `"removed": "end"` later means "delete and archive", with the same request shape. The edge
  history ADR carries the reasons, `as_of` reads, retention and purge.

## Decision options

| Option | For | Against |
|---|---|---|
| **Reconcile by difference on the server (chosen)** | one call, one transaction, readers never see a half-built graph; measured cheap (below) | a new endpoint and a staging path to maintain |
| Keep "delete the run, then reload" as the only way | no new code | a full rebuild for a small change; readers see missing data meanwhile |
| Clients compute the difference | no server change | every consumer writes it; many calls; no atomicity |
| A `replace` flag on `edges:bulk` | no new endpoint | bulk flushes commit per 500 rows (limitation 2), so a replace could never be atomic; overloads a simple endpoint |
| Reconcile with history (`ended_at` in the label tables) now | history questions answered | every read path filtered; the Cypher console and plain AGE copies cannot be filtered (see above) |

## Evidence

Measured on a 940k-edge sample graph (generated data) on PostgreSQL 18 and AGE 1.8. Every write
was rolled back.

| Case | Result |
|---|---|
| `node` scope: one node's 94 edges of one type, 9 removed and 9 added | **3 ms**, one statement |
| `type` scope: a 93k-edge type against a wanted set with 5 % churn | **1.1 s**: 4,564 deleted in 0.44 s, 2,305 inserted in 0.65 s |
| Earlier measurement, 1M edges, 10 % changed: by difference against a full rebuild | **9.2 s** against 88 s |

**How it scales:**
- the `node` scope grows with the node's degree;
- the `type` scope costs one anti-join delete and one insert against the staged set.

## Consequences

- **Consumers:**
  - A re-scan or reload becomes one call per type, or per changed node, instead of a rebuild.
  - The Python client gains `reconcile_edges(...)` and `resolve_edges(mode=...)`.
- **The change feed:**
  - It records removals that used to go unrecorded, because the stale edges were never deleted.
  - Consumers of the feed see more `delete` records after the first reconciliation.
- **Derived data:** community columns and graph metrics are not recomputed by a reconciliation, as
  with any write today (limitation 5).
- **Limitation 4** is removed from the concept once `mode: reconcile` ships. `insert` stays the
  default for one release, then `reconcile` becomes the default, with a changelog note.
- **Not changed:** the one-edge-per-pair rule (limitation 3), and node writes (nodes are upserted
  and deleted as today).

## Acceptance

Tests on PostgreSQL 16 / AGE 1.6 and on PostgreSQL 18 / AGE 1.8:
- **The reload test:** load, change the source, reconcile. The result must equal a fresh load of the
  changed source (nodes, edges, properties). This is the counterpart of
  `test_run_replace_db.py`: there an upsert leaves stale edges, here reconciliation must not.
- **Scope:** edges of the same type to another label, and edges of other types, are untouched.
- **Revisions:** kept-unchanged edges keep their `graphos_rev`; changed edges bump it.
- **Change records:** one `upsert` and one `delete` record, with `detail.reconcile`; ids capped as
  stated.
- **`dry_run`:** it writes nothing.
- **Rule-derived edges:**
  - `edges:resolve` with `mode: reconcile` removes a derived edge whose rule no longer matches;
  - it keeps a consumer-written edge of the same type;
  - `adopt_unmarked` adopts as stated.
- **Measurements:** the `type` scope on a 1M-edge type, within 2× of the evidence above.

## Open questions

1. Edge history: the separate ADR (history table, now measured, see "Edge history" above) in the
   same release, or later?
2. The default of `edges:resolve`: switch to `reconcile` after one release, as proposed?

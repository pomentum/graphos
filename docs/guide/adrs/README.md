# Architecture decision records

An ADR records one decision about graphos: what was decided, why, what else
was considered and what it costs. [`concept.md`](../concept.md) describes how
graphos works today; the ADRs say how it came to work that way.

## When to write one

Write an ADR before the code when a change:

- changes a data-model invariant
  ([services/graphos/DEVELOPMENT.md §3](../../../services/graphos/DEVELOPMENT.md#3-data-model-invariants));
- adds a public surface: an endpoint family, a protocol, a client feature
  other programs rely on;
- changes the supported databases, AGE versions or deployment;
- adds a dependency with licence or security consequences;
- reverses or replaces an earlier decision.

A bug fix, a refactoring or a new endpoint in an existing family does not
need one.

## Files

- One file per decision: `ADR-NNNN-short-slug.md`, numbered in order,
  four digits. A number is never reused.
- ADRs here are public. The [privacy rules](../development.md#7-privacy)
  apply: no customer names, schemas, hosts or paths, and no links to
  `docs/design/`. A draft may start in `docs/design/`; it moves here only
  once it is clean.
- An accepted ADR is not rewritten. A later decision gets a new ADR, and the
  old one's status says `Superseded by ADR-NNNN`.

## Status

| Status | Meaning |
|---|---|
| `Proposed` | Written, under discussion; nothing built on it yet |
| `Accepted` | Decided; the code follows it |
| `Rejected` | Considered and not taken; kept for the reasoning |
| `Superseded by ADR-NNNN` | Replaced by a later decision |
| `Deprecated` | No longer applies, with nothing replacing it |

Each status carries its date: `Accepted (2026-10-16)`.

## Template

```markdown
# ADR-NNNN: Title in plain words

- **Status:** Proposed (YYYY-MM-DD)
- **Scope:** what part of graphos it touches
- **Related:** concept.md sections, other ADRs, upstream issues

## Context

The problem and the constraints, with what is known for a fact.

## Decision

What graphos will do, precisely enough to build it.

## Decision options

The options considered, and why each was taken or left.

## Evidence

Measurements, reproductions, upstream behaviour the decision rests on.

## Consequences

What gets better, what gets worse, what must change with it (docs, client, UI).

## Open questions

What is not decided yet.

## Acceptance

What has to be true for the decision to count as carried out (tests, docs).
```

## Index

| ADR | Title | Status |
|---|---|---|
| [ADR-0001](ADR-0001-edge-reconciliation.md) | Edge reconciliation by difference | Accepted (2026-10-16) |
| [ADR-0002](ADR-0002-mcp-surface.md) | An MCP surface for agents, and a schema summary | Accepted (2026-10-16); its token rules superseded by ADR-0008 |
| [ADR-0003](ADR-0003-variable-length-cypher.md) | Variable-length Cypher patterns in the console | Proposed (2026-10-16) |
| [ADR-0004](ADR-0004-team-annotations.md) | Team annotations: saved views, notes and flags | Proposed (2026-10-16) |
| [ADR-0005](ADR-0005-geo-view-and-external-calls.md) | A geographic view, and no third-party calls from the UI by default | Proposed (2026-10-16), to be built only on demand |
| [ADR-0006](ADR-0006-console-on-by-default.md) | The Cypher console is on by default | Accepted (2026-10-19) |
| [ADR-0007](ADR-0007-drop-postgresql-16.md) | PostgreSQL 16 / AGE 1.6 is no longer supported | Accepted (2026-10-19) |
| [ADR-0008](ADR-0008-mcp-uses-the-api-tokens.md) | `/mcp` takes the tokens the REST API takes, signs its clients in, and is on by default | Accepted (2026-10-19) |
| [ADR-0009](ADR-0009-rename-and-deprecate.md) | Renaming and deprecating labels, edge types and properties | Accepted (2026-10-19) |
| [ADR-0010](ADR-0010-analysis-results-outside-properties.md) | Analysis results outside `properties` | Rejected (2026-10-21): the churn, not the storage, is the cost |

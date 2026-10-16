# Architecture decision records

An ADR records one decision about graphos: what was decided, why, what else
was considered and what it costs. [`concept.md`](../concept.md) describes how
graphos works today; the ADRs say how it came to work that way.

## When to write one

Write an ADR before the code when a change:

- changes a data-model invariant
  ([services/graphos/DEVELOPMENT.md §3](../../services/graphos/DEVELOPMENT.md#3-data-model-invariants));
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
| | None published yet | |

# ADR-0006: The Cypher console is on by default

- **Status:** Accepted (2026-10-19)
- **Scope:** `GRAPHOS_CYPHER`'s default, the console routes, the UI's Query page, the MCP
  `run_cypher` tool
- **Related:** concept.md §3.1 (Cypher / SQL boundary), §8; ADR-0002 (MCP surface);
  ADR-0003 (variable-length patterns)

## Context

The read-only Cypher console came in 0.8.3 as opt-in: `GRAPHOS_CYPHER=true` turned it on, and
without it every console route answered 404. Two reasons held it back then:

- it was new, and its guard was the database's read-only transaction;
- on AGE 1.6 that guard was not enough: `SET`, `REMOVE` and `DELETE` ran inside `BEGIN READ
  ONLY`, so anyone allowed the console could change a tenant's data past revisions and the change
  feed. 0.9.1 fixed it by refusing every write clause before the database, whichever AGE runs.

Since then the console has gained what makes it safe to leave on:

- write clauses, schema-qualified calls, non-Cypher functions, variable-length patterns and
  `vertex_stats()` are refused before the query reaches the database;
- the query runs in `BEGIN READ ONLY`, on a pool of its own that opens no connection until a
  query needs one (`min_size=0`), with `statement_timeout`, a row cap, a watchdog that cancels
  then terminates the backend, `temp_file_limit` where graphos's role may set it, and one query
  at a time per user;
- every query is audited with its text, and needs `GRAPHOS_CYPHER_LEVEL` (`read` by default) on
  the tenant.

Meanwhile the console has become a main way in: the UI's Query page, saved queries
that people share, and, with ADR-0002, `run_cypher` and the saved-query tools for agents. With
the default off, a fresh install shows a Query page that says the console is off, and an agent
gets no Cypher tool and saved-query tools that answer 404.

## Decision

`GRAPHOS_CYPHER` defaults to `true`. An operator turns the console off with
`GRAPHOS_CYPHER=false`; then every console route answers 404, the UI says the console is off
and `/mcp` offers no `run_cypher`, as before. Nothing else changes: the guard, the limits, the
level it needs (`GRAPHOS_CYPHER_LEVEL`, `read`) and the audit stay as they are.

## Decision options

- **Keep it opt-in.** Safe by omission, but every install pays for it with a step, and agents
  and the UI meet a half-working surface. Left.
- **On by default, held to `write`.** Fewer people could query, but read grants exist so people
  can read; a read-only query reads no more than the read routes do. Left.
- **On by default, as it is.** Taken: the guard no longer relies on the database alone, and
  `GRAPHOS_CYPHER=false` is one line for those who do not want it.

## Evidence

- The write-clause guard and its tests (`tests/test_console.py`, `tests/test_console_db.py`) run
  on PostgreSQL 18 / AGE 1.8 and PostgreSQL 16 / AGE 1.6 in CI.
- The console pool is created with `min_size=0`: on by default costs no database connection
  until the first query.

## Consequences

- **Better:** the Query page, saved queries and the agents' Cypher tools work on a fresh install.
- **Worse:** a holder of `read` on a tenant can run any read-only Cypher on it, bounded by the
  console's limits, where before an operator had to agree. A query can still cost up to
  `GRAPHOS_CYPHER_TIMEOUT_S` of a database process; the pool size and the per-user limit bound
  how many run at once.
- **Upgrades:** an install that never set `GRAPHOS_CYPHER` gets the console on. The changelog
  says so under Changed, with `GRAPHOS_CYPHER=false` to keep it off.
- Docs that said "opt-in" or "off unless `GRAPHOS_CYPHER=true`" say "on unless
  `GRAPHOS_CYPHER=false`".

## Open questions

None.

## Acceptance

- `Settings().cypher` is `true`; the tests that check the console off set `cypher=False`.
- The README's Configuration table, concept.md, features.md, usage.md, agents.md, the service
  guide and the changelog say the console is on by default and how to turn it off.

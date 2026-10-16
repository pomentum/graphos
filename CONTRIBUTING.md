# Contributing to graphos

Thank you for helping. Bug reports, questions, documentation fixes and pull requests are all
welcome.

## Before you start

- **A security problem** goes through private reporting, never a public issue: see
  [SECURITY.md](SECURITY.md).
- **A bug or a feature request:** open an issue with the matching template. For a larger change,
  open the issue first, so the design can be agreed before the code is written.
- **A change to the design** (an invariant, a new public surface, the supported databases, or an
  earlier decision reversed) needs an architecture decision record first: read
  [docs/adrs/README.md](docs/adrs/README.md).

## Where the rules are

The rules for working on graphos are written once, for people and coding agents alike:

| Changing | Read |
|---|---|
| Anything | [docs/development.md](docs/development.md): layout, running the stack, docs discipline, test stacks, git, privacy |
| The service (`services/graphos/`) | [services/graphos/DEVELOPMENT.md](services/graphos/DEVELOPMENT.md): where to look, database tests, data-model invariants, Python conventions |
| The Python client (`clients/python/`) | [clients/python/DEVELOPMENT.md](clients/python/DEVELOPMENT.md): models that mirror the service, retries |
| The web UI (`services/graphos-ui/`) | [services/graphos-ui/DEVELOPMENT.md](services/graphos-ui/DEVELOPMENT.md): layers, text, screenshots |
| Diagrams, screenshots, the whitepaper | [docs/development.md, Pictures and docs tools](docs/development.md#pictures-and-docs-tools): never edit a picture by hand |

[AGENTS.md](AGENTS.md) sums up the hard rules for coding agents.

## A pull request

1. Fork the repository and branch from `main`. To bring the branch up to date, merge `main` into
   it; please do not rebase a branch others have seen.
2. Make the change with its tests and its docs. A change is done when its docs are: the
   [changelog](CHANGELOG.md) under `## Unreleased`, and the other files listed in
   [docs/development.md §3](docs/development.md#3-docs-discipline).
3. Run the checks of what you changed (the commands are in each `DEVELOPMENT.md`):

   ```bash
   ruff check services/graphos/src services/graphos/tests && pytest -m "not db" services/graphos/tests
   bash scripts/run-db-tests.sh                                  # service database tests, own compose project
   ruff check clients/python/src clients/python/tests clients/python/examples && pytest clients/python/tests
   (cd services/graphos-ui && npm ci && npm run lint && npm test && npm run build)
   node tools/docs/diagrams.mjs --check && node tools/release/version.mjs --check
   ```

   CI runs the database tests twice, on PostgreSQL 18 / AGE 1.8 and on PostgreSQL 16 / AGE 1.6;
   a change must pass on both.
4. Write commit messages as [conventional commits](https://www.conventionalcommits.org/), in the
   style of `git log`: `type(scope): summary`, lower case, no trailing period
   ([docs/development.md §6](docs/development.md#6-git) lists the types and scopes).
5. Open the pull request and fill in its template.

## Data and privacy

graphos is public. Code, tests, fixtures, commit messages and docs use neutral data only (`acme`,
`Doc`, `LINKS_TO`, the demo ecosystem in `clients/python/examples/`): no names of customers or
projects, no real schemas, hosts, addresses or local paths, and never a password, key or token
([docs/development.md §7](docs/development.md#7-privacy)). Screenshots show the demo tenant only.

## Licence

graphos is licensed under the [Apache License 2.0](LICENSE). Unless you say otherwise, a
contribution you submit is licensed under the same terms (section 5 of the licence).

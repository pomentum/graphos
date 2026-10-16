# AGENTS.md

Instructions for coding agents working on graphos (the
[agents.md](https://agents.md) convention).

**The rules are in [`docs/development.md`](docs/development.md)** and the
guide of the component you change. They are the single source of truth;
read them before changing anything. This file only repeats the hard rules
and the commands.

| Changing | Read |
|---|---|
| Anything | [`docs/development.md`](docs/development.md): layout, docs, test stacks, git, privacy |
| `services/graphos/` | [`docs/development-service.md`](docs/development-service.md): data-model invariants, database tests |
| `clients/python/` | [`docs/development-client.md`](docs/development-client.md): models mirror the service, retries |
| `services/graphos-ui/` | [`docs/development-ui.md`](docs/development-ui.md): layers, text, screenshots |
| `docs/images/`, the whitepaper, API docs assets | [`docs/development.md`, Pictures and docs tools](docs/development.md#pictures-and-docs-tools): never edit a picture by hand |
| A design decision | [`docs/adrs/README.md`](docs/adrs/README.md): when an ADR is needed, its template |

## Hard rules

- **No AI attribution in commits.** Commits are authored by the human
  owner only. Never add a `Co-Authored-By` trailer or any other AI
  attribution.
- **No push, tag, release or merge** without the owner's explicit approval
  in the current turn.
- **No customer data** in code, tests, fixtures, commits or published docs:
  no customer names, schemas, hosts, repository ids or paths. Neutral
  fixtures only.
- **`docs/design/` is never committed**, and committed files do not link to it.
- **Docker test stacks run as their own compose project** (`-p <test-project>`);
  check the project before `down -v`, never touch another project's volumes.
- **Every graph write goes through `writes.py`, in SQL**, with its change
  record in the same transaction. No Cypher writes.

## Commands

```bash
# Service
pip install -e "services/graphos[dev,louvain]"
ruff check services/graphos/src services/graphos/tests
pytest -m "not db" services/graphos/tests
bash scripts/run-db-tests.sh                          # database tests, own compose project

# Client
pip install -e "clients/python[dev]"
ruff check clients/python/src clients/python/tests clients/python/examples
pytest clients/python/tests

# UI
(cd services/graphos-ui && npm ci && npm run lint && npm test && npm run build)

# Repository
node tools/docs/diagrams.mjs --check && node tools/release/version.mjs --check
```

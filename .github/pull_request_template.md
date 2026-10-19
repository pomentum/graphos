## What and why

<!-- What this changes, and the problem it solves. Link the issue: "Closes #...". -->

## How it was checked

<!-- The tests and checks you ran, e.g. pytest, scripts/run-db-tests.sh, npm test, a manual check in the UI. -->

## Checklist

- [ ] Tests for the change, and the checks of the parts it touches pass (see `CONTRIBUTING.md`)
- [ ] `CHANGELOG.md` under `## Unreleased`, and the docs the change touches (`docs/guide/development.md` §3)
- [ ] An API change updates the Python client and its models, and the UI's `src/api/` when the UI uses it
- [ ] A visible UI change retakes the screenshots it affects, of the demo tenant only
- [ ] A design change has an ADR in `docs/guide/adrs/`
- [ ] Neutral data only: no real names, schemas, hosts, local paths, passwords, keys or tokens

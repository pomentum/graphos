# CLAUDE.md

The rules for this repository are in [`docs/guide/development.md`](docs/guide/development.md)
and one guide per component, imported below; [`AGENTS.md`](AGENTS.md)
summarises the hard rules for any agent. Do not repeat them here: change the
guides instead. Before a design change, read [`docs/guide/adrs/README.md`](docs/guide/adrs/README.md).

@docs/guide/development.md
@services/graphos/DEVELOPMENT.md
@clients/python/DEVELOPMENT.md
@services/graphos-ui/DEVELOPMENT.md

## Claude Code

- Use the Bash tool for shell work (git-bash on Windows), not PowerShell.
  `scripts/run-db-tests.sh` already handles Windows paths and Python.
- Do not let the harness add its default `Co-Authored-By: Claude` trailer:
  the no-AI-attribution rule overrides it.
- Ask before `git push`, `git tag`, a release or a merge, every time.
- Run long commands such as `scripts/run-db-tests.sh` in the background.
- This file and its imports are read when a session starts. When the rules
  change during a session, re-read `docs/guide/development.md` and the package
  `DEVELOPMENT.md` before going on.

# Developing graphos

The rules for working on this repository, for people and for coding agents.
This file holds what applies to the whole repository; each component has a
guide of its own:

- **[services/graphos/DEVELOPMENT.md](../../services/graphos/DEVELOPMENT.md)**: the service
  (`services/graphos/`): where to look, database tests, the data-model
  invariants, Python conventions.
- **[clients/python/DEVELOPMENT.md](../../clients/python/DEVELOPMENT.md)**: the Python client
  (`clients/python/`): models that mirror the service, retries, release.
- **[services/graphos-ui/DEVELOPMENT.md](../../services/graphos-ui/DEVELOPMENT.md)**: the web UI
  (`services/graphos-ui/`): layers, text, screenshots.

Together these files are the single source of truth: [`AGENTS.md`](../../AGENTS.md)
and [`CLAUDE.md`](../../CLAUDE.md) point here and do not repeat them.

## 1. Layout

| Path | What it is | Guide |
|---|---|---|
| `services/graphos/` | The service, `graphos-api`: FastAPI on PostgreSQL + Apache AGE (Python 3.11+). | [service](../../services/graphos/DEVELOPMENT.md) |
| `clients/python/` | The Python client, `graphos-client`, published on its own. | [client](../../clients/python/DEVELOPMENT.md) |
| `services/graphos-ui/` | The web UI: React, TypeScript, Vite, Mantine. | [UI](../../services/graphos-ui/DEVELOPMENT.md) |
| `docker-compose.graphos.yml` | The stack: `graphos-db` (PostgreSQL 18 + AGE 1.8), `graphos-api`, `graphos-ui` (profile `ui`), and the move from PostgreSQL 16. | |
| `docker-compose.louvain.yml` | Adds python-igraph (GPL) to a published image at start, for Louvain. | |
| `scripts/` | `init-secrets.sh` / `.ps1`, `run-db-tests.sh`, benchmarks. | |
| `tools/` | `docs/` (diagrams, UI screenshots, API docs assets, the website's page), `release/version.mjs`, `export/ndjson_to_age.py`. | |
| `docs/` | The website's root, as GitHub Pages serves it: `index.html`, `site.css`, `site.js`, `404.html`; no guides. | [§3](#pictures-and-docs-tools) |
| `docs/guide/` | User and design documentation, these guides; [`README.md`](README.md) is its index. | |
| `docs/guide/adrs/` | Architecture decision records: one file per decision. | [ADRs](adrs/README.md) |
| `docs/images/` | Diagrams and UI screenshots, all made by `tools/docs/`, for the README, the guides and the website. | [§3](#pictures-and-docs-tools) |

Where to read about the system as a whole:

- [`concept.md`](concept.md): architecture, write paths, known limitations (§8);
- [`features.md`](features.md): capabilities at a glance;
- [`usage.md`](usage.md): worked requests for every endpoint group;
- [`adrs/`](adrs/README.md): the decisions behind the design, and why;
- [`../CHANGELOG.md`](../../CHANGELOG.md): what changed and when.

## 2. Run the stack, check the repository

Python 3.11, Node 22, Docker with compose.

```bash
bash scripts/init-secrets.sh                                 # random database passwords, once
docker compose -f docker-compose.graphos.yml --profile ui up -d
```

The API listens on `http://127.0.0.1:8080`, the UI on `http://127.0.0.1:8081`.

### Python environment

Work in a venv at `.venv` in the checkout (git ignores it):

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e "services/graphos[dev]" -e "clients/python[dev]"   # .venv/bin/ on Linux and macOS
.venv/Scripts/python -c "import graphos; print(graphos.__file__)"                         # must be in this checkout
```

- Do not test with a global Python: it may hold another checkout's editable
  `graphos`, or a FastAPI that breaks test collection ("Status code 204
  must not have a response body").
- On Windows, a venv under a long path (a temp directory) cannot load
  asyncpg ("The filename or extension is too long"). Keep it at `.venv`.
- pip installs the newest ruff the pin allows, as CI does. Lint with that
  one, not an older ruff on the `PATH`.

### Repository checks

graphos-api, graphos-ui and graphos-client share one version, set by
`node tools/release/version.mjs <version>`. Besides each component's lint
and tests, CI checks the repository as a whole:

```bash
node tools/docs/diagrams.mjs --check                         # generated diagrams up to date
node tools/release/version.mjs --check                       # one version everywhere
python3 tools/docs/api_docs_assets.py --check                # pinned API docs assets
```

and that `LICENSE`, `NOTICE` and `THIRD_PARTY_NOTICES.md` in each package
match the root copies.

## 3. Docs discipline

A change is done when its docs are. With a feature change, update:

- [`CHANGELOG.md`](../../CHANGELOG.md), under `## Unreleased` (Added /
  Changed / Fixed / Deprecated), in the style of the existing entries
  (add the heading at the top when it is missing; the release script dates
  it). Docs that change what a reader learns, such as a new guide or a
  section brought in line with the code, get a `**Docs:**` bullet too;
  retaken screenshots, figures and wording fixes do not;
- [`features.md`](features.md) when a capability is added, changed or removed;
- [`concept.md`](concept.md) where the design is described, and §8 (known
  limitations) when a limitation appears, changes or goes away;
- [`usage.md`](usage.md) for new or changed endpoints;
- the Python client and its models when the API changes
  ([clients/python/DEVELOPMENT.md §5](../../clients/python/DEVELOPMENT.md#5-when-the-client-changes));
- the UI screenshots when the UI changes visibly
  ([services/graphos-ui/DEVELOPMENT.md §5](../../services/graphos-ui/DEVELOPMENT.md#5-when-the-ui-changes));
- an ADR in [`adrs/`](adrs/README.md) before the code, when the change
  alters an invariant, adds a public surface, changes the supported
  databases or reverses an earlier decision.

Each component guide lists what else its changes touch.

### Pictures and docs tools

Every picture in `docs/images/` is made by a tool in `tools/docs/`. Do not
edit one by hand or in a design tool: change the source, run the tool,
commit what it writes. Every picture has alt text that says what it shows.

**Diagrams** (`<name>-light.svg` and `<name>-dark.svg`) are drawn in code by
`tools/docs/diagrams.mjs`, so a label change is a reviewable diff and the
two themes cannot drift apart.

```bash
node tools/docs/diagrams.mjs           # write the SVGs
node tools/docs/diagrams.mjs --check   # CI: exit 1 when a file is not what the script draws
```

- To change a diagram, change its function in the script. A new diagram is
  a function added to `DIAGRAMS`, drawn with the shared `THEMES`.
- Colour carries meaning: blue is graphos, violet is PostgreSQL + Apache
  AGE, red is what a consumer must never do, green is a guarantee.
- In Markdown, show both files with a `<picture>`, as the README does:

  ```html
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/<name>-dark.svg">
    <img src="docs/images/<name>-light.svg" width="100%" alt="What the diagram shows.">
  </picture>
  ```

**UI screenshots** (`ui-<shot>-dark.png`, shown by the README, the docs and
the website) are taken by `tools/docs/screenshots.py` with Playwright, in
dark mode at 1440x900, of the demo tenant only:

```bash
pip install playwright && playwright install chromium   # or --channel msedge / chrome
python clients/python/examples/08_demo_ecosystem.py     # load the demo tenant
export GRAPHOS_USER=... GRAPHOS_PASSWORD=...            # a user granted only the demo tenant
python tools/docs/screenshots.py --ui http://localhost:5173              # every shot
python tools/docs/screenshots.py --ui http://localhost:5173 query        # or name them
python tools/docs/site.py                               # the website's page shows the README's shots
```

The shots are `explore-graph`, `explore-groups`, `explore-map`,
`explore-styles`, `explore-selection`, `schema-editor`, `schema-styles`,
`query` and `csv-import`. A new shot is a function in `SHOTS` of
`screenshots.py`; where it is shown, its alt text says what it shows. Never
take a screenshot of real data.

**The website** (`https://pomentum.github.io/graphos/`) is `docs/` as it is
on `main`: GitHub Pages serves the folder (Settings → Pages → Deploy from a
branch, `main`, `/docs`) and runs no build of ours. Its page,
`docs/index.html`, is generated like the diagrams: `tools/docs/site.py` fills
the frame `tools/docs/site.html` with the README's features, screenshots,
first quickstart block and documentation table, the version, and each
diagram's alt text from its SVG. Edit the README, the diagrams or the frame,
never `docs/index.html`. Beside it sit `site.css` and
`site.js` (its design and light/dark switch), `favicon.svg`,
`404.html` and `.nojekyll`; its pictures are `docs/images/`. The guides
live apart, in `docs/guide/`; GitHub still serves them as plain files, since
everything committed under `docs/` is published, so keep private notes in
`docs/design/`, which git ignores.

What the frame says itself must match the docs: At a glance matches
[`features.md`](features.md) or the README, and each benchmark figure is
quoted, with its setup, from [`apache-age.md`](apache-age.md),
[`concept.md`](concept.md), [`features.md`](features.md) or
[`upgrade-postgresql-18.md`](upgrade-postgresql-18.md); a new measurement
goes into those docs first.

```bash
python tools/docs/site.py                          # write docs/index.html; fails on a broken local link
python tools/docs/site.py --check                  # exit 1 when docs/index.html is out of date
python -m http.server -d docs 8000                 # preview at http://127.0.0.1:8000/
```

Run it after changing the README's features, screenshots, quickstart or
documentation table, or the version (`tools/release/version.mjs`), and
commit `docs/index.html` with the change. The site is live once that commit
is on `main`.

**API docs assets.** graphos serves Swagger UI (`/docs`) and ReDoc
(`/redoc`) from files in `services/graphos/src/graphos/static/api-docs/`,
pinned by version and sha512 in `PINS` of `tools/docs/api_docs_assets.py`.

```bash
python tools/docs/api_docs_assets.py           # fetch the pinned releases, check, write
python tools/docs/api_docs_assets.py --check   # CI: the files match the pins
```

To upgrade, change the pins, run the tool, and update
`THIRD_PARTY_NOTICES.md` at the root and its copies in each package (CI
compares them).

## 4. Line endings

LF everywhere, CRLF only for `*.ps1` (`.gitattributes`). Generated diagrams
are compared byte for byte.

## 5. Test-harness hygiene

A real data volume was lost once because a test run shared a compose
project with a running stack. So:

- Run test stacks as a compose project of their own: always
  `docker compose -p <test-project> ...`. `scripts/run-db-tests.sh` uses
  `graphos-test`, with containers `graphos-test-*`, volumes
  `graphos_test_*` and the API on `127.0.0.1:18089`.
- Before `docker compose ... down -v`, check the project name and list its
  volumes (`docker compose -p <test-project> config --volumes`, or
  `config --format json`). Remove only volumes that belong to the test
  project.
- Never run `down -v`, `docker volume rm` or `docker volume prune` against
  the `graphos` project or any project you did not create. `graphos_data`
  and `graphos_data_pg18` hold real data.

## 6. Git

- **Commits are authored by the human owner only.** Never add a
  `Co-Authored-By` trailer or any other AI attribution, in the message or
  the trailer.
- **No push, tag, release or merge without the owner's explicit approval
  in the current turn.** An earlier approval does not carry over.
- **Never rebase; merge.** Bring a branch up to date by merging `main` into
  it (`git fetch` then `git merge origin/main`, or `git pull --no-rebase`),
  and land it on `main` with a merge or a fast-forward. No `git rebase`,
  `git pull --rebase` or interactive history editing. A merge commit says
  what it joins in plain English, as in `git log`: "Merge 0.9.1 into the
  PostgreSQL 18 branch".
- **Conventional commits**, as in `git log`: `type(scope): summary`, lower
  case, plain English, no trailing period. Types in use: `feat`, `fix`,
  `docs`, `test`, `perf`, `refactor`, `ci`, `chore`. Scopes in use: `ui`,
  `api`, `graph`, `cypher`, `db`, `dataset`, `compose`, `client`, `auth`,
  `tenants`, `schema`, `age`.
  A security fix says so: `fix(cypher): ... (security)`. Releases are
  `chore: release X.Y.Z`.
- **Never commit secrets.** Passwords, keys and tokens live in
  `services/graphos/deploy/secrets/` (made by `scripts/init-secrets.sh`) or
  in `.env` files, and git ignores both; only the `*.example` files and
  `.gitkeep` are tracked there. Never force-add one (`git add -f`), never put a secret in code,
  tests, docs or a commit message, and check `git diff --cached` before a
  commit. A secret that reached a commit is rotated, not just deleted.
- Leave other people's uncommitted changes alone; stage only your own files.

## 7. Privacy

graphos is public. Nothing that identifies a customer goes into code,
tests, fixtures, commit messages or published docs: no customer or project
names, no customer schemas or labels, no hosts, IPs, repository ids or
local paths. Fixtures and examples use neutral data (`acme`, `Doc`,
`LINKS_TO`, the demo ecosystem in `clients/python/examples/`).

## 8. Local working material

`docs/design/` holds local working notes and is never committed. Do not
`git add` it, and do not link to it from committed files. An ADR may be
drafted there; it is published in [`docs/guide/adrs/`](adrs/README.md) only once
it holds nothing private.

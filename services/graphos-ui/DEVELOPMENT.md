# Developing the web UI

How to work on `graphos-ui`, the React UI in `services/graphos-ui/`. The
rules for the whole repository (docs, test stacks, git, privacy) are in
[`development.md`](../../docs/development.md); they apply here too. The
[UI README](README.md) describes its views, its
stack, how its code is organised and how its text is keyed; read it first.

## 1. Where to look

Paths are under `services/graphos-ui/src/`.

| Question | Look in |
|---|---|
| A call to graphos, its types | `api/graphosApi.ts`, `api/types.ts`, `api/http.ts` |
| The live change feed | `api/changeStream.ts` |
| Sign-in, tokens, the chosen tenant | `session/` |
| Data hooks, safe editing with `If-Match` | `data/queries.ts`, `data/useVersionedEditor.ts` |
| The canvas model, layouts, styles | `graph/` |
| A screen | `views/<name>/`, listed in `views/registry.ts` |
| Reusable pieces | `components/` |
| Every piece of text | `i18n/en.json` |
| Colours and the Mantine theme | `theme/` |
| The image and its nginx config | `../Dockerfile`, `../deploy/default.conf.template` |

## 2. Setup, run, test, lint

Node 22. In `services/graphos-ui/`:

```bash
npm ci                                             # from package-lock.json
GRAPHOS_API_URL=http://127.0.0.1:8080 npm run dev  # against a running graphos
npm run lint                                       # prettier --check + tsc -b
npm test                                           # vitest run
npm run build                                      # tsc -b + vite build
npm run format                                     # prettier --write
```

CI runs `npm ci`, `npm run lint`, `npm test` and `npm run build`. The image
workflow builds the UI image and smoke-tests it.

## 3. Rules

- **Layers:** `views → components → data → api`; `graph`, `theme`,
  `session` beside them; all may use `i18n` and `lib`. Only `api/` talks to
  graphos; components fetch through `data/` hooks.
- **Nothing tenant-specific in the code.** The UI reads labels, types,
  properties, styles and indexes from the API (`schema`, `graph/map`,
  `communities/map`, `catalogue`). A tenant's shape never needs a UI change.
- **Edits keep their version.** Saves go through `useVersionedEditor`:
  only changed fields, with `If-Match`, so someone else's save is never
  overwritten.
- **All text in `i18n/en.json`**, keyed by layer (`common.*`, `shell.*`,
  `views.<view>.*`, `components.<component>.*`, `notices.*`, `errors.*`).
  Keys are typed; a test checks every key used exists. Plurals use
  `_one` / `_other`.
- **No requests to other origins.** Fonts and icons are bundled and nginx
  sends a strict Content-Security-Policy. Do not add CDNs or remote assets.
- **Dependencies pinned** to exact versions in `package.json`, installed
  with `npm ci`. Adding one updates `THIRD_PARTY_NOTICES.md` when its
  licence needs a notice.
- **A new screen:** `views/<name>/<Name>View.tsx`, an entry in
  `views/registry.ts`, its id mapped in `App.tsx` (see the UI README).

## 4. Code conventions

- **Prettier** (`.prettierrc.json`, print width 120) and **TypeScript
  strict** (`noUnusedLocals`, `noUnusedParameters`,
  `noUncheckedIndexedAccess`).
- Styles in a CSS module next to the component (`Name.module.css`), colours
  from `theme/` tokens.
- Pure logic goes in `lib/` or `graph/`, outside React, where it is easy to
  test.
- Comments explain a reason the code cannot show; a short `/** ... */` on
  exported functions and types. No narrating comments.
- **Tests** live in `services/graphos-ui/tests/<area>.test.ts` (or
  `.test.tsx` when they render), with Vitest and Testing Library;
  `renderWithTheme` from `tests/render.tsx` renders inside the app's theme.

## 5. When the UI changes

Besides the shared list in [development.md §3](../../docs/development.md#3-docs-discipline):

- **Retake the screenshots** a visible change affects, of the demo tenant
  only, with `tools/docs/screenshots.py`, then embed them in the whitepaper
  with `node tools/docs/diagrams.mjs`. The steps are in
  [development.md, Pictures and docs tools](../../docs/development.md#pictures-and-docs-tools).
- Update the UI README when a view, a folder's job or the stack changes.
- A UI that needs a new endpoint changes the service first
  ([services/graphos/DEVELOPMENT.md](../graphos/DEVELOPMENT.md)).
- A change to the layers, the stack or the no-other-origins rule gets an
  [ADR](../../docs/adrs/README.md) first.

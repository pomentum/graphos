# graphos UI

A web UI for graphos: see what a tenant's graph is made of, explore it on a canvas, edit nodes
and relationships, find the paths between two nodes, browse a label as a table, extend the
schema, and follow changes live.

It has no backend of its own. The image is static files served by nginx, which also forwards
`/api/` to graphos on the same origin, so graphos needs no CORS setup. Every user signs in with
their own graphos token; the token is kept in the browser tab only (`sessionStorage`) and is
what graphos checks on every call, so read-only tokens get a read-only UI and every edit is
recorded under the user's name in the audit log and the change feed. A network blip on reload
does not sign anyone out: only a refused token is forgotten.

## Views

| View | What it shows |
|---|---|
| **Explore · Map** | One bubble per label (sized by its nodes) and a line per pair of linked labels (thicker with more edges). Click a label for its relationships in and out; start from its best-connected nodes, or browse it. A tenant of more than 5,000 nodes opens here. |
| **Explore · Groups** | One bubble per community of the last Louvain recompute, named after its best-connected member, with the relationships between groups. Click a group for what it is made of; open its hub or browse its members. Administrators can compute the groups (a background job). |
| **Explore · Graph** | The canvas. A small tenant opens on its whole graph. Double-click (or E) adds a node's neighbours 20 at a time with a "+N more" node, fanned out beside it; the camera follows and the rest fades. Back (Ctrl/⌘+Z) undoes any step; Collapse (C), Hide (H), Focus (F: only the neighbourhood), Escape. Colour by label or by group; inspect and edit node and relationship properties; add and delete nodes and relationships. |
| **Path** | Every shortest path between two nodes, picked by search on any property; the search lives in the address, so it can be shared and survives leaving the view. |
| **Overview** | Counts and sizes per label and relationship type, and how the values of one property are spread. |
| **Activity** | The change feed, live, with who made each change; a change to one node opens it. |
| **Browse** | One label's nodes as a table: a property filter, columns to choose, paging, a row opens in Explore. |
| **Schema** | Labels as a tree under their parents (counts, properties), relationship types (label pairs, counts), search indexes. Or as a **diagram**: a grid of label boxes, each label holding the labels that inherit from it in a block, busiest first (laid out by ELK); select a label to draw its relationship lines with types and edge counts, or turn on All lines. Boxes can be moved and put back with Tidy up. Administrators can add labels, relationship types (also by dragging from one label to another on the diagram) and search indexes; nothing is removed. |

Search (top bar) opens with `/`, or ⌘K on Apple devices; Return opens the top hit even before
the results arrive. Edits are saved with `If-Match` and only for the fields changed; a node
saved by someone else meanwhile is flagged with who saved it instead of being overwritten.
Light and dark themes; narrow windows and phones get overlay panels.

## Run it

With the rest of graphos (from the repository root):

```bash
docker compose -f docker-compose.graphos.yml --profile ui up -d
```

The UI is then at <http://127.0.0.1:8081> (`GRAPHOS_UI_PORT` changes the port). On its own,
point it at any graphos:

```bash
docker run -p 8081:8080 -e GRAPHOS_API_UPSTREAM=http://graphos-api:8080 ghcr.io/pomentum/graphos-ui
```

For development, with graphos running on port 8080:

```bash
npm ci
GRAPHOS_API_URL=http://127.0.0.1:8080 npm run dev
```

`npm test` runs the tests, `npm run lint` checks formatting and types, `npm run format` formats.

## What graphos needs to provide

Nothing UI-specific. The UI reads the tenant's shape from the API and adapts to it: labels,
relationship types and search indexes from `GET /v1/{tenant}/schema`, counts and links from the
graph map (`GET …/graph/map`), groups from `GET …/graph/communities/map`, property names from the
catalogue and from the nodes themselves (`nodes:scan`). A label or property added to a tenant
appears in the UI without a UI change. Groups appear once Louvain has run (the `louvain` extra).

## How the code is organised

Each folder has one job; the arrows show which may use which.

```
views  →  components  →  data  →  api
  ↓           ↓           ↓
graph       theme      session      (all may use i18n and lib)
```

| Folder | Responsibility |
|---|---|
| `api/` | Talking to graphos and nothing else: `HttpClient` (token, JSON, `ETag`, errors), `GraphosApi` (one typed method per endpoint), `ChangeStream` (the live change feed, resumed after a dropped connection). |
| `session/` | The signed-in token and the chosen tenant (`SessionProvider`), kept for the tab. |
| `data/` | React Query hooks over `GraphosApi` (`queries.ts`); the shared live activity (`ActivityProvider`); safe editing (`useVersionedEditor`: edits keep the version they started from and send only changed fields with `If-Match`). |
| `graph/` | Canvas models, independent of React: `GraphModel` (nodes and edges on screen, with an undo history), `canvasStyle` (colours, fading, which nodes are labelled), `graphMap` and `communityGraph` (the label map and the groups as graphs), `WorkspaceProvider` (the Explore canvas, mode and selection). |
| `components/` | Reusable pieces with no data fetching of their own beyond hooks: canvas, inspectors, property editor, search field, dialogs, table, layout. |
| `views/` | One folder per screen, assembled from components and data hooks. `registry.ts` lists the screens; the sidebar and the routes are built from it. |
| `i18n/` | Every piece of text (`en.json`) and the i18next set-up. |
| `theme/` | Colour tokens (black and greys with the graphos blue) and the Mantine theme. |
| `lib/` | Small pure helpers: property diffs and typing, paging, text shortening, change descriptions, time and size formatting. |

To add a screen: create `views/<name>/<Name>View.tsx`, add an entry to `views/registry.ts`
(menu group, icon, who may open it, the i18n key of its title), and map its id to the component
in `App.tsx`.

## Text and translations

All text the UI shows is in [`src/i18n/en.json`](src/i18n/en.json), keyed by where it is used,
layer first:

| Key | Holds |
|---|---|
| `common.*` | Words shared by many screens: actions (Save, Cancel, …), states, counts |
| `shell.*` | The frame: sign-in, toolbar, sidebar, tenant switcher, account |
| `views.<view>.<area>.<name>` | One screen, e.g. `views.explore.actions.back` |
| `components.<component>.*` | A reusable component, e.g. `components.inspector.hide` |
| `notices.*`, `errors.*` | Confirmations and what went wrong |

Components read text with `useTranslation()`; code outside React imports `t` from `src/i18n`.
Counts use i18next plurals (`_one` / `_other`) and values are interpolated as `{{name}}`. Keys
are typed from `en.json`, so a misspelt key does not compile, and a test checks that every key
used exists. A language is added as another JSON file with the same keys.

## Stack

React 19, TypeScript, Vite, Mantine 8 (components and theming), TanStack Query (server state),
React Router, sigma.js 3 with graphology (WebGL graph drawing, ForceAtlas2 layout in a web
worker), i18next, Vitest and Testing Library. Fonts (Inter, IBM Plex Mono) are bundled, so the
UI makes no requests to other origins; nginx sends a strict Content-Security-Policy.

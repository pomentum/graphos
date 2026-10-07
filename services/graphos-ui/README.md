# graphos UI

A web UI for graphos: search a tenant's graph, explore it on a canvas, edit nodes and edges,
find the paths between two nodes, see what a tenant holds, and follow changes live.

Explore opens on the whole graph (`GET …/graph/export`), laid out in a web worker; a tenant of
more than 5,000 nodes shows a sample of that size, and search takes you to any node. Search
opens with Ctrl K (⌘K on Apple devices) or `/`.

It has no backend of its own. The image is static files served by nginx, which also forwards
`/api/` to graphos on the same origin, so graphos needs no CORS setup. Every user signs in with
their own graphos token; the token is kept in the browser tab only (`sessionStorage`) and is
what graphos checks on every call, so read-only tokens get a read-only UI and every edit is
recorded under the user's name in the audit log and the change feed.

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

Nothing UI-specific. The UI reads the tenant's shape from the API and adapts to it: labels and
relationship types from `GET /v1/{tenant}/schema`, the search index from the same schema,
properties from the nodes themselves (`nodes:scan`), and counts from `/stats`. A label or
property added to a tenant appears in the UI without a UI change.

## How the code is organised

Each folder has one job; the arrows show which may use which.

```
views  →  components  →  data  →  api
  ↓           ↓           ↓
graph       theme      session
```

| Folder | Responsibility |
|---|---|
| `api/` | Talking to graphos and nothing else: `HttpClient` (token, JSON, `ETag`, errors), `GraphosApi` (one typed method per endpoint), `ChangeStream` (the live change feed, resumed after a dropped connection). |
| `session/` | The signed-in token and the chosen tenant (`SessionProvider`), kept for the tab. |
| `data/` | React Query hooks over `GraphosApi` (`queries.ts`); the shared live activity (`ActivityProvider`); safe node editing (`useNodeEditor`: edits keep the version they started from and send only changed fields with `If-Match`). |
| `graph/` | The canvas model, independent of React: `GraphModel` (nodes and edges on screen), `canvasStyle` (which nodes are blue or labelled), `WorkspaceProvider` (the Explore canvas and selection). |
| `components/` | Reusable pieces with no data fetching of their own beyond hooks: canvas, inspector, property editor, search field, dialogs, layout. |
| `views/` | One folder per screen, assembled from components and data hooks. `registry.ts` lists the screens; the sidebar and the routes are built from it. |
| `theme/` | Colour tokens (black and greys with the graphos blue) and the Mantine theme. |
| `lib/` | Small pure helpers: property diffs, expand rules, change descriptions, time and size formatting. |

To add a screen: create `views/<name>/<Name>View.tsx`, add an entry to `views/registry.ts`
(menu group, icon, who may open it), and map its id to the component in `App.tsx`.

## Stack

React 19, TypeScript, Vite, Mantine 8 (components and theming), TanStack Query (server state),
React Router, sigma.js 3 with graphology (WebGL graph drawing), Vitest and Testing Library.
Fonts (Inter, IBM Plex Mono) are bundled, so the UI makes no requests to other origins; nginx
sends a strict Content-Security-Policy.

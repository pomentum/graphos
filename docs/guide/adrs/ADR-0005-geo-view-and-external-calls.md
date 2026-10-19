# ADR-0005: A geographic view, and no third-party calls from the UI by default

- **Status:** Proposed (2026-10-16), to be built only on demand
- **Scope:**
  - the tenant spec's `display` (where coordinates come from);
  - a new Explore view, and the UI's Content-Security-Policy;
  - an optional tile source.
- **Related:** the existing Explore "Map" view (a map of labels and
  communities, not geography; this ADR names the new one "Geo" to avoid the clash)

## Context

**Some tenants hold places:** sites, offices, data centres, suppliers' premises. They want them on
a map, with the graph's selection shared between the map and the canvas.

**How graph tools usually do it:**
- a MapLibre map;
- addresses **geocoded from the browser** by sending them to a public geocoding service. That
  sends tenant data to a third party, and is a data leak in any regulated setting.

**The UI today makes no third-party calls.** Its nginx sets a strict policy:

```
default-src 'self'; connect-src 'self'; img-src 'self' data:; …
```

Map tiles are third-party by nature, unless the operator hosts them.

## Decision

### 1. Coordinates come from the data, declared in the label's display

`VertexDisplay` gains `geo`:
- **two properties:** `{"lat": "<property>", "lon": "<property>"}`, decimal degrees (WGS 84);
- **or one property** holding `[lon, lat]`: `{"point": "<property>"}`.

**Validation:**
- the property definitions can declare the ranges (−90..90, −180..180);
- a node whose values are missing or out of range is left off the map, and counted ("12 nodes
  without coordinates").

**graphos never geocodes.** A consumer that has addresses geocodes them in its own pipeline and
writes coordinates.

### 2. A Geo view in Explore

**Which labels:** the labels with `geo` in their display.

**The points:**
- drawn with the label's colour and icon;
- clustered when dense (MapLibre's GeoJSON clustering, in a worker).

**Selection** is shared with the canvas through the workspace, as the other Explore views do.
Selecting on the map and switching to the canvas shows the same nodes, and the reverse.

**Data:**
- **The first version:** the view loads the geo labels' coordinates through `nodes:scan`
  (properties limited to the coordinates and the caption), up to `GRAPHOS_UI_GEO_MAX_POINTS`
  (default 50,000), and says when it stops.
- **A bounding-box read** (`nodes:within`, on generated numeric columns with a B-tree index) only
  if tenants exceed that. Measured: numeric-column sorts and filters are 4–5× faster than agtype
  ones.

**The library:**
- maplibre-gl 6 (BSD-3-Clause) and react-map-gl 8 (MIT);
- loaded as a separate chunk only when the Geo view opens, so the main bundle does not grow by
  about 800 KB.

### 3. The base map, and no third-party calls by default

| Source | Setting | Calls |
|---|---|---|
| **Default:** a bundled world outline (Natural Earth 1:110m, public domain, about 200 KB GeoJSON), drawn as a plain style in the theme's colours | none | none: everything from graphos's own origin |
| Self-hosted vector tiles: a PMTiles file the operator mounts, served by the UI's nginx with range requests (pmtiles, BSD-3-Clause) | `GRAPHOS_UI_GEO_PMTILES=/tiles/world.pmtiles` | none: same origin |
| A tile server the operator chooses (their own, or a provider under contract) | `GRAPHOS_UI_GEO_STYLE_URL=https://tiles.example.com/style.json` | to that origin only |

**With a tile server:**
- The UI's nginx adds that origin, and only that one, to `connect-src` and `img-src` (and
  `worker-src` if needed).
- The policy is generated at container start from the setting. With no setting, the policy is
  unchanged.

**Attribution:** the style's attribution (for example © OpenStreetMap contributors, ODbL) is
always shown on the map.

### 4. The general rule this ADR records

**The web UI makes no request to a third party unless an operator configures it.** That means no
fonts, analytics, error reporting, tile or geocoding services by default. Any future feature that
needs an outside origin must:
- name a setting;
- extend the policy only by that origin;
- be documented under privacy in `installation.md`.

## Decision options

| Option | For | Against |
|---|---|---|
| **Coordinates from data, a bundled outline by default, tiles only by operator setting (chosen)** | works offline and air-gapped; nothing leaves the deployment unless chosen; the strict policy stays the default | the default base map is plain (outlines, no streets) |
| Public OpenStreetMap tiles by default | a detailed map out of the box | every viewer's IP and viewport go to a third party; the OSM tile usage policy forbids heavy use; the policy must allow an outside origin by default |
| Browser geocoding | addresses work without coordinates | sends tenant data to a third party; rate limits; wrong matches presented as facts |
| Server-side geocoding in graphos | no browser leak | graphos would call outside services (an outbound dependency graphos avoids), and own a geocoder's licence and quality |
| No geo view: consumers export and use a GIS tool | no work | the selection is not shared with the graph; the most-asked view of place data is missing |

## Consequences

- **The tenant spec:** `display.vertex[label].geo` is a new optional field. Old specs are
  unchanged, and the schema check validates the named properties.
- **The UI:**
  - a Geo view, shown only when some label has `geo`;
  - the bundle gains a lazily loaded map chunk;
  - `THIRD_PARTY_NOTICES.md` gains maplibre-gl, react-map-gl, pmtiles and Natural Earth.
- **The UI image:** its entrypoint renders the CSP from the settings, which is a small templating
  step that exists today.
- **Documentation:**
  - a Geo section in `features.md`;
  - the three base-map options and their privacy effect in `installation.md`;
  - the `geo` display field in the schema reference.
- **Exports:** the SVG and PNG exports do not include the base map in the first version. A map
  image export uses MapLibre's canvas, and is subject to the tile source's terms.

## Acceptance

- **The default:** with no geo setting, opening the Geo view makes requests to the UI's own origin
  only. A browser test lists every request.
- **A tile server:** with `GRAPHOS_UI_GEO_STYLE_URL`, the generated policy allows that origin and
  no other. A request to another origin is blocked.
- **Coordinates:**
  - nodes without coordinates or out of range are counted, not drawn;
  - both the `lat`/`lon` and the `point` forms work.
- **Selection:** a selection made on the map is the canvas's selection, and the reverse.
- **Bundle:** the main bundle does not grow by more than 10 KB, and the map chunk loads only on
  the Geo view.
- **Scale:** 50,000 points render and cluster smoothly on a mid-range laptop. The frame time is
  measured.

## Open questions

1. Is there a tenant that needs this now? If not, the ADR stays Proposed and nothing is built.
2. Is the bundled outline enough as the default, or should the image also carry a small PMTiles
   extract (larger image, still no outside calls)?

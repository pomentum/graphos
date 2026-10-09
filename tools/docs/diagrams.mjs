#!/usr/bin/env node
/**
 * diagrams.mjs — the diagrams in the README and the whitepaper, drawn as SVG.
 *
 *   node tools/docs/diagrams.mjs          write docs/images/<name>-light.svg and <name>-dark.svg
 *   node tools/docs/diagrams.mjs --check  exit 1 if any file differs from what this draws
 *
 * It also embeds the web UI screenshots (docs/images/ui-*.jpg, taken by
 * tools/docs/screenshots.py) in the whitepaper, and --check fails when the page holds
 * other pictures than the committed ones.
 *
 * Each diagram comes in two files because GitHub picks one per reader's theme through a
 * <picture> element, and the whitepaper page embeds both and lets its own theme choose. The
 * drawing lives in code rather than in a design tool, so a label change is a reviewable diff
 * and the light and dark versions cannot drift apart.
 *
 * Colour carries meaning: blue is graphos, violet is PostgreSQL + Apache AGE, red is what a
 * consumer is never allowed to do, green is a property graphos guarantees.
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const OUT = join(ROOT, 'docs', 'images');

// The same palette as docs/whitepaper.html: flat fills, hairline strokes, no shadows.
const THEMES = {
  light: {
    ink: '#131A22', soft: '#4A5563', rule: '#C4CDD6', card: '#F7F8F9', well: '#E6EAEE',
    accent: '#1D5BD6', accentSoft: '#E3EBFB', accentLine: '#9DB7EC',
    store: '#5B45B0', storeSoft: '#ECE8F8', storeLine: '#B8ACE3',
    ok: '#1C7A3A', okSoft: '#DCEFE2', okLine: '#9FD0AE',
    warn: '#9A6100', warnSoft: '#F6EAD2', warnLine: '#E0BE7C',
    stop: '#B3261E', stopSoft: '#F7DEDC', stopLine: '#E5A6A1',
    arrow: '#6B7584',
  },
  dark: {
    ink: '#E6EAEF', soft: '#9AA5B3', rule: '#2E3642', card: '#161A20', well: '#1C2129',
    accent: '#8AB0FF', accentSoft: '#1A2640', accentLine: '#3A5690',
    store: '#B7A6F2', storeSoft: '#231E3A', storeLine: '#594C92',
    ok: '#6FCB8D', okSoft: '#132A1C', okLine: '#2F6B42',
    warn: '#E6B75A', warnSoft: '#2E2410', warnLine: '#7A5E1E',
    stop: '#F2877F', stopSoft: '#361816', stopLine: '#7A3434',
    arrow: '#8C97A6',
  },
};

const FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif";
const MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace";

// ── drawing kit ──────────────────────────────────────────────────────────────

function kit(t) {
  const heads = new Map();
  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const k = {
    t,
    text(x, y, s, o = {}) {
      return `<text x="${x}" y="${y}" font-size="${o.size || 14}" font-weight="${o.weight || 400}" fill="${o.fill || t.ink}"${o.anchor ? ` text-anchor="${o.anchor}"` : ''}${o.mono ? ` font-family="${MONO}"` : ''}${o.ls ? ` letter-spacing="${o.ls}"` : ''}>${esc(s)}</text>`;
    },
    pill(x, y, w, label, fill, color, o = {}) {
      return `<rect x="${x}" y="${y}" width="${w}" height="24" rx="3" fill="${fill}"${o.line ? ` stroke="${o.line}"` : ''}/>${k.text(x + w / 2, y + 16.5, label, { size: 12.5, weight: 600, fill: color, anchor: 'middle', mono: o.mono })}`;
    },
    card(x, y, w, h, o = {}) {
      return `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${o.r ?? 4}" fill="${o.fill || t.card}" stroke="${o.stroke || t.rule}" stroke-width="${o.sw || 1.2}"${o.dash ? ` stroke-dasharray="${o.dash}"` : ''}/>`;
    },
    arrow(d, o = {}) {
      const color = o.color || t.arrow;
      if (!heads.has(color)) heads.set(color, `h${heads.size}`);
      const id = heads.get(color);
      return `<path d="${d}" fill="none" stroke="${color}" stroke-width="${o.w || 1.8}"${o.dash ? ` stroke-dasharray="${o.dash}"` : ''} stroke-linecap="round" marker-end="url(#${id})"/>`;
    },
    cylinder(cx, top, rx, h, o = {}) {
      const ry = o.ry || 16;
      return `<g><path d="M${cx - rx} ${top} v${h} a${rx} ${ry} 0 0 0 ${2 * rx} 0 v-${h}" fill="${o.fill || t.storeSoft}" stroke="${o.line || t.storeLine}" stroke-width="1.4"/>
  <ellipse cx="${cx}" cy="${top}" rx="${rx}" ry="${ry}" fill="${o.fill || t.storeSoft}" stroke="${o.line || t.storeLine}" stroke-width="1.4"/></g>`;
    },
    // A short line with a mark in front: a cross for a cost, a tick for a guarantee.
    mark(x, y, s, kind) {
      const c = kind === 'ok' ? t.ok : t.stop;
      const glyph = kind === 'ok'
        ? `<path d="M${x} ${y - 4} l4 4 l8 -9" fill="none" stroke="${c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`
        : `<path d="M${x + 1} ${y - 9} l9 9 M${x + 10} ${y - 9} l-9 9" stroke="${c}" stroke-width="2" stroke-linecap="round"/>`;
      return `${glyph}${k.text(x + 20, y, s, { size: 13.5 })}`;
    },
    defs() {
      return `<defs>
  ${[...heads].map(([color, id]) => `<marker id="${id}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="${color}"/></marker>`).join('\n  ')}
</defs>`;
    },
    svg(w, h, title, desc, body) {
      return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" font-family="${FONT}" role="img" aria-labelledby="title desc">
<title id="title">${esc(title)}</title>
<desc id="desc">${esc(desc)}</desc>
${k.defs()}
${body}
</svg>
`;
    },
  };
  return k;
}

// ── 1. the problem, and the shape of the answer ─────────────────────────────

function problem(t) {
  const k = kit(t);
  const { text, card, arrow, cylinder, mark } = k;
  const rows = [70, 160, 250];
  const body = `
${text(30, 40, 'EVERY SERVICE TALKS TO THE DATABASE', { size: 12, weight: 600, fill: t.soft, ls: 1.2, mono: true })}
${rows.map((y, i) => `${card(30, y, 180, 68)}
${text(48, y + 28, `Service ${'ABC'[i]}`, { size: 16, weight: 600 })}
${text(48, y + 50, 'driver · DSN · Cypher', { size: 12, fill: t.soft, mono: true })}`).join('\n')}
${arrow('M210 104 C 250 104, 262 170, 296 182', { color: t.stop })}
${arrow('M210 194 H296', { color: t.stop })}
${arrow('M210 284 C 250 284, 262 218, 296 206', { color: t.stop })}
${cylinder(380, 100, 80, 190)}
${text(380, 186, 'graph', { size: 16, weight: 700, fill: t.store, anchor: 'middle' })}
${text(380, 206, 'database', { size: 16, weight: 700, fill: t.store, anchor: 'middle' })}
${text(380, 232, 'one shared login', { size: 12, fill: t.soft, anchor: 'middle' })}
${mark(30, 352, 'credentials and query text in every service', 'stop')}
${mark(30, 378, 'no tenant boundary; one slow query stalls all', 'stop')}
${mark(30, 404, 'each team re-solves bulk load and indexing', 'stop')}
<path d="M495 30 V410" stroke="${t.rule}" stroke-width="1.2"/>
${text(525, 40, 'ONE SERVICE OWNS THE GRAPH', { size: 12, weight: 600, fill: t.accent, ls: 1.2, mono: true })}
${rows.map((y, i) => `${card(525, y, 130, 68)}
${text(541, y + 28, `Service ${'ABC'[i]}`, { size: 16, weight: 600 })}
${text(541, y + 50, 'typed HTTP', { size: 12, fill: t.soft, mono: true })}`).join('\n')}
${arrow('M655 104 C 672 104, 676 150, 690 160', { color: t.accent })}
${arrow('M655 194 H688', { color: t.accent })}
${arrow('M655 284 C 672 284, 676 238, 690 228', { color: t.accent })}
${card(692, 70, 116, 248, { fill: t.accentSoft, stroke: t.accentLine })}
${text(750, 172, 'graphos', { size: 17, weight: 700, fill: t.accent, anchor: 'middle' })}
${text(750, 194, 'auth · tenants', { size: 12, fill: t.soft, anchor: 'middle' })}
${text(750, 212, 'limits · metrics', { size: 12, fill: t.soft, anchor: 'middle' })}
${arrow('M808 194 H842', { color: t.store, w: 2.2 })}
${cylinder(905, 100, 60, 190)}
${text(905, 186, 'Postgres', { size: 15, weight: 700, fill: t.store, anchor: 'middle' })}
${text(905, 206, '+ Apache AGE', { size: 13, weight: 600, fill: t.store, anchor: 'middle' })}
${text(905, 232, 'private network', { size: 12, fill: t.soft, anchor: 'middle' })}
${mark(525, 352, 'one account per consumer', 'ok')}
${mark(525, 378, 'a graph per tenant; bounded, timed queries', 'ok')}
${mark(525, 404, 'bulk load, indexes, communities built in', 'ok')}`;
  return k.svg(1000, 430, 'From many database clients to one graph service',
    'Left: three services each embed a database driver, a connection string and their own Cypher, and all reach one shared graph database, so credentials and query text spread, there is no tenant boundary, a slow query affects everyone and each team re-solves bulk load and indexing. Right: the same services call graphos over typed HTTP; graphos alone reaches PostgreSQL with Apache AGE on a private network, giving each consumer its own account and short-lived tokens, a graph per tenant with bounded, timed queries, and built-in bulk load, indexing and community detection.',
    body);
}

// ── 2. architecture ─────────────────────────────────────────────────────────

function architecture(t) {
  const k = kit(t);
  const { text, pill, card, arrow, cylinder } = k;
  const consumers = [
    ['Loader job', 'NDJSON / COPY bulk writes', 86],
    ['Application', 'search · neighbors · expand', 172],
    ['Analytics / UI', 'aggregates · export', 258],
    ['Operator', 'admin: users, grants', 344],
  ];
  const layers = [
    ['token → user → tenant level', '403 / 404 before any SQL'],
    ['typed REST contracts', 'Pydantic models, OpenAPI'],
    ['SQL built inside', 'identifiers checked, limits capped'],
    ['statement_timeout per query', 'a bad query cannot starve the pool'],
    ['metrics · JSON logs · audit', 'Prometheus, W3C traceparent'],
  ];
  const body = `
${text(30, 40, 'CONSUMERS', { size: 12, weight: 600, fill: t.soft, ls: 1.2, mono: true })}
${text(470, 40, 'GRAPHOS-API', { size: 12, weight: 600, fill: t.accent, ls: 1.2, mono: true, anchor: 'middle' })}
${text(840, 40, 'GRAPHOS-DB', { size: 12, weight: 600, fill: t.store, ls: 1.2, mono: true, anchor: 'middle' })}
${consumers.map(([n, d, y]) => `${card(30, y - 26, 196, 62)}
${text(46, y, n, { size: 15, weight: 600 })}
${text(46, y + 22, d, { size: 12, fill: t.soft })}`).join('\n')}
${consumers.map(([, , y]) => arrow(`M226 ${y + 5} C 262 ${y + 5}, 270 ${215}, 304 ${215}`, { color: t.accent })).join('\n')}
${text(30, 444, 'graphos-net', { size: 12, weight: 600, fill: t.accent, mono: true })}${text(120, 444, 'HTTP + bearer token, the only way in', { size: 12, fill: t.soft })}
${card(306, 58, 330, 360, { fill: t.accentSoft, stroke: t.accentLine })}
${text(326, 88, 'graphos-api', { size: 18, weight: 700, fill: t.accent })}
${text(326, 108, 'FastAPI · asyncpg · one process per replica', { size: 12.5, fill: t.soft })}
${layers.map(([a, b], i) => {
  const y = 124 + i * 50;
  return `${card(324, y, 294, 42)}
${text(338, y + 18, a, { size: 13, weight: 600 })}
${text(338, y + 34, b, { size: 11.5, fill: t.soft })}`;
}).join('\n')}
${pill(324, 380, 140, 'Louvain · igraph', t.card, t.accent)}${pill(472, 380, 146, 'K/V cache · LRU', t.card, t.accent)}
${arrow('M636 215 H708', { color: t.store, w: 2.2 })}
${text(712, 444, 'graphos-internal', { size: 12, weight: 600, fill: t.store, mono: true })}${text(842, 444, 'no external port', { size: 12, fill: t.soft })}
${cylinder(840, 82, 128, 316, { ry: 22 })}
${text(840, 132, 'PostgreSQL 16', { size: 16, weight: 700, fill: t.store, anchor: 'middle' })}
${text(840, 152, '+ Apache AGE (openCypher)', { size: 13, weight: 600, fill: t.store, anchor: 'middle' })}
${[['tenant_a', 'its own AGE graph'], ['tenant_b', 'its own AGE graph'], ['graphos_meta', 'registrations · cache']].map(([n, d], i) => {
  const y = 176 + i * 62;
  return `<rect x="734" y="${y}" width="212" height="50" rx="4" fill="${t.card}" stroke="${t.storeLine}" stroke-opacity="0.7"/>
${text(750, y + 21, n, { size: 13, weight: 600, mono: true })}
${text(750, y + 39, d, { size: 12, fill: t.soft })}`;
}).join('\n')}
${text(840, 384, 'pg_trgm · pg_stat_statements', { size: 12, fill: t.soft, anchor: 'middle', mono: true })}
<rect x="30" y="464" width="940" height="34" rx="3" fill="${t.stopSoft}"/>
<path d="M48 475 l12 12 M60 475 l-12 12" stroke="${t.stop}" stroke-width="2" stroke-linecap="round"/>
${text(72, 486, 'Consumers never receive a database connection string and never send raw Cypher or SQL.', { size: 13.5, weight: 600, fill: t.stop })}`;
  return k.svg(1000, 516, 'How graphos is deployed',
    'Consumers such as a loader job, an application, an analytics UI and an operator call graphos-api over the graphos-net network with a short-lived bearer token from signing in. graphos-api checks the grants of the signed-in user against the tenant in the path and the level each operation needs, validates typed request contracts, builds SQL internally with checked identifiers and capped limits, runs each query under a statement timeout, and emits metrics, JSON logs and an audit trail; it also runs Louvain community detection, in a separate process, and a key/value cache. Only graphos-api reaches graphos-db, PostgreSQL 16 with Apache AGE, over an internal network with no external port. Each tenant is its own AGE graph, and graphos_meta holds registrations and the cache. Consumers never receive a connection string and never send raw Cypher or SQL.',
    body);
}

// ── 3. community detection ──────────────────────────────────────────────────

function communities(t) {
  const k = kit(t);
  const { text, card, arrow } = k;
  const steps = [
    ['Edge stream', 'plain SQL,', 'integer ids only'],
    ['Build graph', 'vertices in id', 'order, weighted'],
    ['Louvain', 'own process,', 'fixed seed'],
    ['Stable ids', 'Jaccard ≥ 0.5', 'vs. the last run'],
    ['Write back', 'COPY + one', 'UPDATE … FROM'],
    ['Denormalise', 'community onto', 'every edge'],
  ];
  const W = 142, G = 21;
  const body = `
${text(30, 40, 'POST /v1/{tenant}/recompute-communities', { size: 12.5, weight: 600, fill: t.accent, mono: true })}
${steps.map(([a, b, c], i) => {
  const x = 30 + i * (W + G);
  const store = i === 0 || i >= 4;
  return `${card(x, 62, W, 96, store ? { fill: t.storeSoft, stroke: t.storeLine } : { fill: t.accentSoft, stroke: t.accentLine })}
${text(x + 14, 94, a, { size: 15.5, weight: 700, fill: store ? t.store : t.accent })}
${text(x + 14, 118, b, { size: 12.5, fill: t.soft })}
${text(x + 14, 136, c, { size: 12.5, fill: t.soft })}
${i < steps.length - 1 ? arrow(`M${x + W + 2} 110 H${x + W + G - 3}`) : ''}`;
}).join('\n')}
<rect x="30" y="180" width="940" height="1" fill="${t.rule}"/>
${text(30, 210, 'one run per tenant at a time (advisory lock, 409 otherwise)', { size: 13 })}
${text(530, 210, 'dry_run previews the partition without writing', { size: 13 })}
${text(30, 236, 'response reports time per step and modularity', { size: 13 })}
${text(530, 236, 'ids persist across runs as membership drifts', { size: 13 })}
<rect x="30" y="256" width="12" height="12" rx="3" fill="${t.storeSoft}" stroke="${t.storeLine}"/>${text(48, 266, 'inside PostgreSQL', { size: 12.5, fill: t.soft })}
<rect x="190" y="256" width="12" height="12" rx="3" fill="${t.accentSoft}" stroke="${t.accentLine}"/>${text(208, 266, 'inside graphos-api', { size: 12.5, fill: t.soft })}`;
  return k.svg(1000, 282, 'How communities are computed',
    'A call to recompute-communities, by a user with maintain on the tenant, streams edges out of PostgreSQL as integer ids with plain SQL, builds a weighted undirected igraph with its vertices in id order, runs Louvain in a separate process with a fixed seed, and matches each new cluster to its predecessor from the last run by Jaccard overlap of at least 0.5 so community ids stay stable. Results are written back with COPY and a single UPDATE, then the community id is denormalised onto every edge so scoped traversals use an index. Only one run per tenant happens at a time, a dry run previews the partition, and the response reports time per step and modularity.',
    body);
}

const DIAGRAMS = { problem, architecture, communities };

// The whitepaper carries each picture as inline SVG, so the page is one self-contained file
// that renders in any HTML viewer: opened from disk, attached to a message, in a preview pane
// that blocks data: URIs, or served from anywhere. The page holds <!--fig:<name>-<theme>-->
// markers and this script fills them. Ids are prefixed per picture because six SVGs share one
// document, and the dark copy is hidden from assistive tech since it repeats the light one.
const PAGE = join(ROOT, 'docs', 'whitepaper.html');
const embedded = new Map();

function inline(svg, fig, theme) {
  return svg
    .replace('<svg ', `<svg class="pic-${theme}"${theme === 'dark' ? ' aria-hidden="true"' : ''} `)
    .replace(/ width="\d+" height="\d+"/, '')
    .replace('aria-labelledby="title desc"', `aria-labelledby="${fig}-title ${fig}-desc"`)
    .replace(/id="(title|desc|h\d+)"/g, `id="${fig}-$1"`)
    .replace(/url\(#(h\d+)\)/g, `url(#${fig}-$1)`)
    .trimEnd();
}

// The UI screenshots: photographs, so they cannot be inline SVG. The page carries the
// JPEG copies as data: URIs, which keeps it one file; where a viewer blocks data: URIs only
// these pictures are missing. The alt text says what each one shows.
const SHOTS = {
  'ui-explore-graph-dark': 'The Explore view of the demo tenant, an invented software ecosystem: about 450 nodes coloured by the community Louvain found and sized by PageRank, six clusters around a core of shared libraries. The core library corelog is selected; its panel shows what graphos computed for it (its group, PageRank, betweenness, degree and component), its properties and its relationships.',
  'ui-explore-groups-dark': 'The Groups mode of Explore: one bubble per community of the last Louvain recompute, named after its best-connected member, with lines for the relationships between communities.',
  'ui-explore-map-dark': 'The Map mode of Explore: one bubble per label, sized by its nodes, and a line per pair of labels that are linked.',
  'ui-schema-editor-dark': 'The schema diagram of the demo tenant. Library, Framework and Tool sit in a block under their parent label Package; the selected label, Library, shows its relationship types to the other labels and, in a panel, its properties and relationships. The Add menu offers a new label, relationship type or search index.',
};
for (const [name, alt] of Object.entries(SHOTS)) {
  let jpeg;
  try { jpeg = readFileSync(join(OUT, `${name}.jpg`)); } catch { continue; }
  embedded.set(name, `<img class="shot" src="data:image/jpeg;base64,${jpeg.toString('base64')}" width="1440" height="900" loading="lazy" alt="${alt}">`);
}

const check = process.argv.includes('--check');
let stale = 0;
mkdirSync(OUT, { recursive: true });
for (const [name, draw] of Object.entries(DIAGRAMS)) {
  for (const [theme, t] of Object.entries(THEMES)) {
    const file = join(OUT, `${name}-${theme}.svg`);
    const svg = draw(t);
    embedded.set(`${name}-${theme}`, inline(svg, `${name}-${theme}`, theme));
    if (check) {
      let cur = '';
      try { cur = readFileSync(file, 'utf8').replaceAll('\r\n', '\n'); } catch {}
      if (cur !== svg) { console.error(`diagrams: docs/images/${name}-${theme}.svg is not what this draws`); stale++; }
    } else {
      writeFileSync(file, svg);
      console.log(`diagrams: wrote docs/images/${name}-${theme}.svg`);
    }
  }
}
{
  // Compare ignoring line endings, so a CRLF checkout does not fail --check.
  const page = readFileSync(PAGE, 'utf8').replaceAll('\r\n', '\n');
  const next = page.replace(/<!--(fig|shot):([a-z-]+)-->[\s\S]*?<!--\/\1:\2-->/g, (m, kind, fig) => {
    if (!embedded.has(fig)) throw new Error(`whitepaper.html: unknown ${kind === 'fig' ? 'figure' : 'screenshot'} "${fig}"`);
    return `<!--${kind}:${fig}-->${embedded.get(fig)}<!--/${kind}:${fig}-->`;
  });
  if (check) {
    if (next !== page) { console.error('diagrams: docs/whitepaper.html embeds stale pictures'); stale++; }
  } else if (next !== page) {
    writeFileSync(PAGE, next);
    console.log('diagrams: embedded pictures in docs/whitepaper.html');
  }
}
if (check) {
  if (stale) { console.error('Run: node tools/docs/diagrams.mjs'); process.exit(1); }
  console.log('diagrams: every file matches');
}

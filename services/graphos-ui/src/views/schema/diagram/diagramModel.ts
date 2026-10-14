import type { ElkNode } from "elkjs/lib/elk-api";
import type { GraphMap, TenantSchema } from "../../../api/types";
import { Endpoints, type EndpointStatus, type Pair } from "../../../lib/endpoints";

/** One label of the schema, drawn as a box. */
export interface Box {
  label: string;
  /** Nodes of this label itself; null while the counts are unknown. */
  count: number | null;
  parent: string | null;
  /** The top of its inheritance chain (itself when it has no parent). */
  family: string;
  /** No nodes yet (drawn greyed). */
  empty: boolean;
  w: number;
  h: number;
}

/** One relationship type on a line: its edges, and how it stands against the declarations. */
export interface LinkType {
  type: string;
  /** Edges in the data; 0 for a declared pair no edge uses yet. */
  count: number;
  estimated: boolean;
  status: EndpointStatus;
  /** The declared pair covering it (this pair, or one for its labels' parents). */
  via: Pair | null;
}

/** One line: the relationship types from one label to another, or a label's parent. */
export interface Link {
  key: string;
  src: string;
  dst: string;
  kind: "relationship" | "inherits";
  /** Relationship types on this line, declared first, then most edges. */
  types: LinkType[];
  /** Edges on this line, all types together. */
  count: number;
  /**
   * The line as a whole: `undeclared` if any type on it is outside its
   * declared pairs, else `declared` if any is declared, else `open`.
   */
  status: EndpointStatus;
  /** The caption drawn on the line. */
  caption: string;
}

/**
 * A label and everything that inherits from it, drawn as one block: its own
 * box first, then its children, a child with children of its own as a
 * block inside. Containment shows inheritance, so it needs no lines.
 */
export interface Block {
  root: string;
  items: (string | Block)[];
  /** Labels in the block at any depth, the root included. */
  size: number;
}

export interface Diagram {
  boxes: Box[];
  /** Every relationship line (drawn on demand) and every `inherits` link (shown by the blocks). */
  links: Link[];
  /** Labels that others inherit from, as blocks, largest first. */
  blocks: Block[];
  /** Labels in no block, busiest first, empty ones last. */
  lone: string[];
  /** Registered relationship types no edge uses yet: they have no line. */
  unusedTypes: string[];
}

export interface Point {
  x: number;
  y: number;
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** A line as ELK routed it, in diagram coordinates: border to border through its bends, and its caption's centre. */
export interface Route {
  points: Point[];
  caption: Point | null;
}

/**
 * Where ELK put things, in diagram coordinates: each box's and each block's
 * top-left corner and size, and each routed line (by its key).
 */
export interface Layout {
  positions: Record<string, Point>;
  blocks: Record<string, Rect>;
  routes: Record<string, Route>;
}

export type Positions = Record<string, Point>;

const BOX_HEIGHT = 48;
const CHAR_WIDTH = 7.2;
const BOX_PADDING = 32;
const MIN_BOX_WIDTH = 110;
/** Relationship types named on a line's caption; the rest are counted. */
const CAPTION_TYPES = 2;
/** Room for a block's title above its labels. */
export const BLOCK_TITLE = 26;

/** A box wide enough for its name and its count line. */
export function boxSize(label: string, meta: string): { w: number; h: number } {
  const chars = Math.max(label.length, meta.length * 0.85);
  return { w: Math.max(MIN_BOX_WIDTH, Math.round(chars * CHAR_WIDTH + BOX_PADDING)), h: BOX_HEIGHT };
}

/**
 * A line's caption: its first types with counts (declared first), then how
 * many more. `counts: false` leaves the counts out (a tenant without edges,
 * where every one would read 0).
 */
export function linkCaption(
  types: Pick<LinkType, "type" | "count" | "estimated">[],
  formatCount: (count: number) => string,
  more: (count: number) => string,
  counts = true,
): string {
  const named = types
    .slice(0, CAPTION_TYPES)
    .map((t) => (counts ? `${t.type} ${t.estimated ? "≈" : ""}${formatCount(t.count)}` : t.type));
  const rest = types.length - CAPTION_TYPES;
  return rest > 0 ? `${named.join(" · ")} ${more(rest)}` : named.join(" · ");
}

export interface DiagramText {
  /** A box's node count as written on it, e.g. "1.2k nodes". */
  nodes: (count: number) => string;
  formatCount: (count: number) => string;
  /** "+3" more types on a caption. */
  more: (count: number) => string;
}

const PLAIN_TEXT: DiagramText = {
  nodes: String,
  formatCount: String,
  more: (count) => `+${count}`,
};

/** Room on a box's second line for " · 12 properties", which arrives after the counts. */
const META_ROOM = " · 12 properties";

/** The top of a label's inheritance chain (cycle-safe). */
function familyOf(label: string, hierarchy: Record<string, string>): string {
  const seen = new Set([label]);
  let current = label;
  for (let parent = hierarchy[current]; parent && !seen.has(parent); parent = hierarchy[current]) {
    seen.add(parent);
    current = parent;
  }
  return current;
}

/** Busiest first, empty last, then by name: the order labels take in the grid. */
function byWeight(a: Box, b: Box): number {
  return (b.count ?? 0) - (a.count ?? 0) || a.label.localeCompare(b.label);
}

/**
 * The schema as a grid: a box per registered label (its own node count from
 * the graph map); a label others inherit from holds them in its block
 * (largest blocks first), the other labels follow, busiest first.
 * Every declared label pair, and every pair edges join in the data, gets a
 * line carrying its types, each marked declared, outside the declarations
 * (`undeclared`) or `open` (its type declares no pairs); a child gets an
 * `inherits` link to its parent. A label's line to itself is kept (drawn
 * as a loop).
 */
export function buildDiagram(
  schema: TenantSchema,
  map: GraphMap | undefined,
  options: { hideEmpty?: boolean; text?: DiagramText } = {},
): Diagram {
  const text = options.text ?? PLAIN_TEXT;
  const counts = map ? new Map(map.labels.map((l) => [l.label, l.count] as const)) : null;
  const boxes: Box[] = [];
  for (const label of [...schema.vertex_labels].sort()) {
    const count = counts ? (counts.get(label) ?? 0) : null;
    const empty = count === 0;
    if (options.hideEmpty && empty) continue;
    const parent = schema.label_hierarchy[label] ?? null;
    const family = familyOf(label, schema.label_hierarchy);
    const meta = (count === null ? "" : text.nodes(count)) + META_ROOM;
    boxes.push({ label, count, parent, family, empty, ...boxSize(label, meta) });
  }
  const shown = new Set(boxes.map((b) => b.label));

  const endpoints = new Endpoints(schema);
  const byPair = new Map<string, Link>();
  const lineFor = (src: string, dst: string): Link => {
    const key = `rel|${src}|${dst}`;
    const line = byPair.get(key) ?? {
      key,
      src,
      dst,
      kind: "relationship" as const,
      types: [],
      count: 0,
      status: "open" as EndpointStatus,
      caption: "",
    };
    byPair.set(key, line);
    return line;
  };
  const used = new Set<string>();
  for (const link of map?.links ?? []) {
    used.add(link.type);
    if (!shown.has(link.src_label) || !shown.has(link.dst_label)) continue;
    const line = lineFor(link.src_label, link.dst_label);
    line.types.push({
      type: link.type,
      count: link.count,
      estimated: link.estimated,
      ...endpoints.check(link.type, link.src_label, link.dst_label),
    });
    line.count += link.count;
  }
  // Declared pairs no edge uses yet are part of the definition too.
  for (const [type, pairs] of endpoints.declared) {
    for (const pair of pairs) {
      if (!shown.has(pair.src) || !shown.has(pair.dst)) continue;
      const line = lineFor(pair.src, pair.dst);
      if (line.types.some((t) => t.type === type)) continue;
      line.types.push({ type, count: 0, estimated: false, status: "declared", via: pair });
    }
  }
  const rank: Record<EndpointStatus, number> = { declared: 0, undeclared: 1, open: 2 };
  // Without a single edge, every count would read 0: the captions name the types only.
  const withCounts = (map?.links ?? []).some((l) => l.count > 0);
  for (const line of byPair.values()) {
    line.types.sort((a, b) => rank[a.status] - rank[b.status] || b.count - a.count || a.type.localeCompare(b.type));
    line.status = line.types.some((t) => t.status === "undeclared")
      ? "undeclared"
      : line.types.some((t) => t.status === "declared")
        ? "declared"
        : "open";
    line.caption = linkCaption(line.types, text.formatCount, text.more, withCounts);
  }

  // Children by parent, among the labels shown (a hidden parent leaves its children on their own).
  const kids = new Map<string, Box[]>();
  for (const box of boxes) {
    if (box.parent === null || box.parent === box.label || !shown.has(box.parent)) continue;
    kids.set(box.parent, [...(kids.get(box.parent) ?? []), box]);
  }
  const placed = new Set<string>();
  const itemOf = (box: Box): string | Block => {
    placed.add(box.label);
    const children = (kids.get(box.label) ?? []).filter((c) => !placed.has(c.label)).sort(byWeight);
    children.forEach((c) => placed.add(c.label));
    if (children.length === 0) return box.label;
    const items = [box.label, ...children.map(itemOf)];
    return {
      root: box.label,
      items,
      size: items.reduce((n, item) => n + (typeof item === "string" ? 1 : item.size), 0),
    };
  };
  const roots = boxes.filter((b) => b.parent === null || b.parent === b.label || !shown.has(b.parent));
  // A parent cycle has no root: its first label stands in for one.
  const top = [...roots, ...boxes.filter((b) => !roots.includes(b))].flatMap((b) =>
    placed.has(b.label) ? [] : [itemOf(b)],
  );

  const byLabel = new Map(boxes.map((b) => [b.label, b] as const));
  const blocks = top.filter((item): item is Block => typeof item !== "string");
  const total = (block: Block): number =>
    block.items.reduce((n, item) => n + (typeof item === "string" ? (byLabel.get(item)?.count ?? 0) : total(item)), 0);
  blocks.sort((a, b) => b.size - a.size || total(b) - total(a) || a.root.localeCompare(b.root));
  const lone = top
    .filter((item): item is string => typeof item === "string")
    .map((label) => byLabel.get(label)!)
    .sort((a, b) => Number(a.empty) - Number(b.empty) || byWeight(a, b));

  // A box's colour comes from the outermost block it sits in.
  const colourBy = (item: string | Block, family: string): void => {
    if (typeof item === "string") byLabel.get(item)!.family = family;
    else item.items.forEach((inner) => colourBy(inner, family));
  };
  blocks.forEach((block) => colourBy(block, block.root));
  lone.forEach((box) => (box.family = box.label));

  const inherits: Link[] = boxes
    .filter((b) => b.parent !== null && b.parent !== b.label && shown.has(b.parent))
    .map((b) => ({
      key: `inh|${b.label}|${b.parent}`,
      src: b.label,
      dst: b.parent!,
      kind: "inherits" as const,
      types: [],
      count: 0,
      status: "declared" as const,
      caption: "",
    }));

  return {
    boxes,
    links: [...inherits, ...[...byPair.values()].sort((a, b) => a.key.localeCompare(b.key))],
    blocks,
    lone: lone.map((b) => b.label),
    unusedTypes: schema.edge_labels.filter((type) => !used.has(type)).sort(),
  };
}

/** A block as nested lists of labels, for comparing layouts. */
function shape(item: string | Block): unknown {
  return typeof item === "string" ? item : item.items.map(shape);
}

/**
 * What the layout depends on: the boxes and their sizes, the blocks and the
 * order of everything. Equal keys lay out the same, so counts or properties
 * that change the text but not the sizes do not lay the diagram out again.
 */
export function layoutKey(diagram: Diagram): string {
  return JSON.stringify([
    diagram.boxes.map((b) => [b.label, b.w, b.h]),
    diagram.blocks.map(shape),
    diagram.lone,
    isLayered(diagram) ? routedLinks(diagram).map((l) => [l.key, captionRoom(l)]) : [],
  ]);
}

/**
 * The lines the layout routes: each relationship line between two labels,
 * except those of types that declare no pairs (drawn only on request, as
 * free curves). A label's line to itself is drawn as a loop.
 */
export function routedLinks(diagram: Diagram): Link[] {
  return diagram.links.filter((l) => l.kind === "relationship" && l.src !== l.dst && l.status !== "open");
}

/**
 * The caption room a routed line keeps: its first types with room for large
 * counts, so new counts do not lay the diagram out again.
 */
export function captionRoom(link: Link): number {
  const named = link.types.slice(0, CAPTION_TYPES).map((t) => `${t.type} ≈000,000`);
  const more = link.types.length > CAPTION_TYPES ? " +99" : "";
  return Math.round(captionRect(named.join(" · ") + more, { x: 0, y: 0 }).w);
}

/** What is selected on the diagram: a label's box or a line. */
export type Selection = { kind: "label"; id: string } | { kind: "link"; id: string } | null;

/**
 * What stays bright while something is selected: a label, its lines and the
 * labels at their other ends; a line and its two labels. Null: nothing is
 * selected, everything is drawn as usual.
 */
export function focusOf(diagram: Diagram, selection: Selection): { labels: Set<string>; links: Set<string> } | null {
  if (!selection) return null;
  if (selection.kind === "link") {
    const link = diagram.links.find((l) => l.key === selection.id);
    return link ? { labels: new Set([link.src, link.dst]), links: new Set([link.key]) } : null;
  }
  const labels = new Set([selection.id]);
  const links = new Set<string>();
  for (const link of diagram.links) {
    if (link.src !== selection.id && link.dst !== selection.id) continue;
    links.add(link.key);
    labels.add(link.src);
    labels.add(link.dst);
  }
  return { labels, links };
}

/** A colour per outermost block, largest first; lone labels get none. */
export function familyColours(blocks: Block[], palette: readonly string[]): Map<string, string> {
  return new Map(blocks.slice(0, palette.length).map((block, i) => [block.root, palette[i]!]));
}

/**
 * Up to this many labels and lines, the diagram follows the relationships
 * (`LAYERED_OPTIONS`); a larger schema is packed into a tidy grid, which
 * stays readable where a layered drawing grows thousands of pixels wide and
 * takes seconds to lay out.
 */
export const LAYERED_MAX_LABELS = 40;
export const LAYERED_MAX_LINES = 80;

/**
 * Labels placed in layers along their lines, top to bottom (near the
 * screen's shape), so lines run between boxes rather than across them,
 * routed at right angles with room kept for their captions; blocks are laid
 * out with the rest (their lines cross into them). Labels without lines are
 * packed beside.
 */
export const LAYERED_OPTIONS: Record<string, string> = {
  "elk.algorithm": "layered",
  "elk.direction": "DOWN",
  "elk.hierarchyHandling": "INCLUDE_CHILDREN",
  "elk.edgeRouting": "ORTHOGONAL",
  "elk.json.edgeCoords": "ROOT",
  "elk.layered.spacing.nodeNodeBetweenLayers": "56",
  "elk.layered.spacing.edgeNodeBetweenLayers": "20",
  "elk.spacing.nodeNode": "28",
  "elk.spacing.edgeEdge": "12",
  "elk.spacing.edgeNode": "16",
  "elk.spacing.edgeLabel": "4",
  "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
  "elk.padding": "[top=0,left=0,bottom=0,right=0]",
};
const BLOCK_PADDING = `[top=${BLOCK_TITLE + 8},left=12,bottom=12,right=12]`;

/** Blocks and labels packed into rows, in order, near the screen's shape; the same inside a block. */
export function packOptions(padding: string): Record<string, string> {
  return {
    "elk.algorithm": "rectpacking",
    "elk.aspectRatio": "1.6",
    "elk.spacing.nodeNode": "14",
    "elk.padding": padding,
  };
}

export const GRID_OPTIONS = { ...packOptions("[top=0,left=0,bottom=0,right=0]"), "elk.spacing.nodeNode": "28" };

/** Whether a diagram is drawn along its relationships (else as a grid). */
export function isLayered(diagram: Diagram): boolean {
  return diagram.boxes.length <= LAYERED_MAX_LABELS && routedLinks(diagram).length <= LAYERED_MAX_LINES;
}

export const familyId = (root: string) => `family:${root}`;

/**
 * The diagram as an ELK graph: blocks (nested as inheritance nests), the
 * lone labels and, laid out along the relationships, the routed lines; a
 * large schema as a grid, without lines (they are drawn as free curves).
 */
export function toElkGraph(diagram: Diagram): ElkNode {
  const layered = isLayered(diagram);
  const size = new Map(diagram.boxes.map((b) => [b.label, b] as const));
  const item = (it: string | Block): ElkNode =>
    typeof it === "string"
      ? { id: it, width: size.get(it)!.w, height: size.get(it)!.h }
      : {
          id: familyId(it.root),
          layoutOptions: layered ? { "elk.padding": BLOCK_PADDING } : packOptions(BLOCK_PADDING),
          children: it.items.map(item),
        };
  const children = [...diagram.blocks, ...diagram.lone].map(item);
  if (!layered) return { id: "schema", layoutOptions: GRID_OPTIONS, children };
  return {
    id: "schema",
    layoutOptions: LAYERED_OPTIONS,
    children,
    edges: routedLinks(diagram).map((link) => ({
      id: link.key,
      sources: [link.src],
      targets: [link.dst],
      labels: [{ id: `${link.key}|caption`, text: link.key, width: captionRoom(link), height: CAPTION_HEIGHT }],
    })),
  };
}

/** Every box's and block's place, and every line's route, out of ELK's answer, in diagram coordinates. */
export function readElkLayout(laid: ElkNode): Layout {
  const layout: Layout = { positions: {}, blocks: {}, routes: {} };
  const walk = (node: ElkNode, offset: Point) => {
    for (const child of node.children ?? []) {
      const at = { x: offset.x + (child.x ?? 0), y: offset.y + (child.y ?? 0) };
      if (child.id.startsWith("family:")) {
        layout.blocks[child.id] = { ...at, w: child.width ?? 0, h: child.height ?? 0 };
        walk(child, at);
      } else {
        layout.positions[child.id] = at;
      }
    }
  };
  walk(laid, { x: 0, y: 0 });
  for (const edge of laid.edges ?? []) {
    const section = edge.sections?.[0];
    if (!section) continue;
    const label = edge.labels?.[0];
    layout.routes[edge.id] = {
      points: [section.startPoint, ...(section.bendPoints ?? []), section.endPoint].map(({ x, y }) => ({ x, y })),
      caption:
        label?.x !== undefined && label.y !== undefined
          ? { x: label.x + (label.width ?? 0) / 2, y: label.y + (label.height ?? 0) / 2 }
          : null,
    };
  }
  return layout;
}

/** A routed line as a path: straight runs with rounded corners. */
export function routePath(points: Point[], radius = 8): string {
  if (points.length === 0) return "";
  const [first, ...rest] = points;
  let d = `M ${first!.x} ${first!.y}`;
  for (let i = 0; i < rest.length - 1; i++) {
    const prev = points[i]!;
    const at = rest[i]!;
    const next = rest[i + 1]!;
    const r = Math.min(
      radius,
      Math.hypot(at.x - prev.x, at.y - prev.y) / 2,
      Math.hypot(next.x - at.x, next.y - at.y) / 2,
    );
    const toward = (p: Point) => {
      const len = Math.hypot(p.x - at.x, p.y - at.y) || 1;
      return { x: at.x + ((p.x - at.x) / len) * r, y: at.y + ((p.y - at.y) / len) * r };
    };
    const a = toward(prev);
    const b = toward(next);
    d += ` L ${a.x} ${a.y} Q ${at.x} ${at.y} ${b.x} ${b.y}`;
  }
  const last = points[points.length - 1]!;
  return `${d} L ${last.x} ${last.y}`;
}

/** The point halfway along a route, for a caption ELK did not place. */
export function routeMiddle(points: Point[]): Point {
  const lengths = points.slice(1).map((p, i) => Math.hypot(p.x - points[i]!.x, p.y - points[i]!.y));
  let left = lengths.reduce((a, b) => a + b, 0) / 2;
  for (const [i, length] of lengths.entries()) {
    if (left <= length) {
      const a = points[i]!;
      const b = points[i + 1]!;
      const t = length ? left / length : 0;
      return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
    }
    left -= length;
  }
  return points[0] ?? { x: 0, y: 0 };
}

/** Where the line from a box's centre towards `toward` leaves the box. */
export function exitPoint(box: Rect, toward: Point): Point {
  const dx = toward.x - box.x;
  const dy = toward.y - box.y;
  if (dx === 0 && dy === 0) return { x: box.x, y: box.y };
  const sx = dx === 0 ? Number.POSITIVE_INFINITY : box.w / 2 / Math.abs(dx);
  const sy = dy === 0 ? Number.POSITIVE_INFINITY : box.h / 2 / Math.abs(dy);
  const s = Math.min(sx, sy);
  return { x: box.x + dx * s, y: box.y + dy * s };
}

/** A quadratic curve from `a` to `b` bent towards `c`. */
export interface Curve {
  a: Point;
  c: Point;
  b: Point;
}

/** The point at `t` (0..1) along a curve. */
export function pointAt(curve: Curve, t: number): Point {
  const u = 1 - t;
  return {
    x: u * u * curve.a.x + 2 * u * t * curve.c.x + t * t * curve.b.x,
    y: u * u * curve.a.y + 2 * u * t * curve.c.y + t * t * curve.b.y,
  };
}

/**
 * A relationship line (boxes given by centre and size): a gentle curve from
 * border to border, bowed further when its reverse also exists so the two
 * do not overlap; a label's line to itself is a loop above it (no curve).
 */
export function freeGeometry(
  src: Rect,
  dst: Rect,
  self: boolean,
  bowed: boolean,
): { d: string; caption: Point; curve: Curve | null } {
  if (self) {
    const top = src.y - src.h / 2;
    const left = src.x + src.w / 2 - 34;
    return {
      d: `M ${left} ${top} C ${left - 10} ${top - 46}, ${left + 40} ${top - 46}, ${left + 30} ${top}`,
      caption: { x: left + 10, y: top - 46 },
      curve: null,
    };
  }
  const mid = { x: (src.x + dst.x) / 2, y: (src.y + dst.y) / 2 };
  const nx = -(dst.y - src.y);
  const ny = dst.x - src.x;
  const len = Math.hypot(nx, ny) || 1;
  const bend = Math.min(60, len * 0.08) + (bowed ? 24 : 0);
  const control = { x: mid.x + (nx / len) * bend, y: mid.y + (ny / len) * bend };
  const a = exitPoint(src, control);
  const b = exitPoint(dst, control);
  const curve = { a, c: control, b };
  return { d: `M ${a.x} ${a.y} Q ${control.x} ${control.y} ${b.x} ${b.y}`, caption: pointAt(curve, 0.5), curve };
}

const CAPTION_HEIGHT = 18;
const CAPTION_CHAR_WIDTH = 6.4;
const CAPTION_PADDING = 14;
/** Where along a line a caption may sit, best first. */
const CAPTION_STEPS = [0.5, 0.4, 0.6, 0.3, 0.7, 0.22, 0.78];
/** A loop's caption may rise above its usual place, in these steps. */
const LOOP_STEPS = [0, -20, -40];

/** The space a caption takes on the canvas (centre and size). */
export function captionRect(text: string, at: Point): Rect {
  return { x: at.x, y: at.y, w: text.length * CAPTION_CHAR_WIDTH + CAPTION_PADDING, h: CAPTION_HEIGHT };
}

function overlap(a: Rect, b: Rect): number {
  const w = Math.min(a.x + a.w / 2, b.x + b.w / 2) - Math.max(a.x - a.w / 2, b.x - b.w / 2);
  const h = Math.min(a.y + a.h / 2, b.y + b.h / 2) - Math.max(a.y - a.h / 2, b.y - b.h / 2);
  return w > 0 && h > 0 ? w * h : 0;
}

/** A caption to place: its text and the line it belongs to. */
export interface CaptionInput {
  key: string;
  text: string;
  /** The line's curve, or null for a loop (placed at `at`). */
  curve: Curve | null;
  at: Point;
  /** Placed first: the selection's lines, then busier ones. */
  priority: number;
}

/**
 * Places captions so they do not cover each other or the label boxes: each
 * takes the first spot along its line (middle first) that is clear, or the
 * least covered one; captions with higher priority choose first.
 */
export function placeCaptions(inputs: CaptionInput[], boxes: Rect[]): Record<string, Point> {
  const placed: Rect[] = [];
  const out: Record<string, Point> = {};
  for (const input of [...inputs].sort((a, b) => b.priority - a.priority || a.key.localeCompare(b.key))) {
    const spots = input.curve
      ? CAPTION_STEPS.map((t) => pointAt(input.curve!, t))
      : LOOP_STEPS.map((dy) => ({ x: input.at.x, y: input.at.y + dy }));
    let best = spots[0]!;
    let bestCost = Number.POSITIVE_INFINITY;
    for (const spot of spots) {
      const rect = captionRect(input.text, spot);
      const cost =
        placed.reduce((sum, other) => sum + 3 * overlap(rect, other), 0) +
        boxes.reduce((sum, box) => sum + overlap(rect, box), 0);
      if (cost < bestCost) {
        best = spot;
        bestCost = cost;
        if (cost === 0) break;
      }
    }
    placed.push(captionRect(input.text, best));
    out[input.key] = best;
  }
  return out;
}

// Positions the user arranged, per tenant, for the life of the page: graphos
// keeps nothing in the browser's storage, so a reload lays the diagram out
// again.  Positions of labels in a block are relative to the block.
const ARRANGED = new Map<string, Positions>();

/** Positions the user arranged for this tenant since the page was opened. */
export function loadPositions(tenant: string): Positions {
  return { ...(ARRANGED.get(tenant) ?? {}) };
}

/** Remembers positions while the page is open. */
export function savePositions(tenant: string, positions: Positions): void {
  if (Object.keys(positions).length === 0) ARRANGED.delete(tenant);
  else ARRANGED.set(tenant, { ...positions });
}

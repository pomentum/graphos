import type { ElkNode } from "elkjs/lib/elk-api";
import type { GraphMap, TenantSchema } from "../../../api/types";

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

/** One line: the relationship types from one label to another, or a label's parent. */
export interface Link {
  key: string;
  src: string;
  dst: string;
  kind: "relationship" | "inherits";
  /** Relationship types on this line, most edges first. */
  types: { type: string; count: number; estimated: boolean }[];
  /** Edges on this line, all types together. */
  count: number;
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

/** Where ELK put things, in diagram coordinates: each box's and each block's top-left corner and size. */
export interface Layout {
  positions: Record<string, Point>;
  blocks: Record<string, Rect>;
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

/** A line's caption: its busiest types with counts, then how many more. */
export function linkCaption(
  types: Link["types"],
  formatCount: (count: number) => string,
  more: (count: number) => string,
): string {
  const named = types.slice(0, CAPTION_TYPES).map((t) => `${t.type} ${t.estimated ? "≈" : ""}${formatCount(t.count)}`);
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
 * Every pair of labels that edges join gets a line carrying its types; a
 * child gets an `inherits` line to its parent. A label's line to itself is
 * kept (drawn as a loop).
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

  const byPair = new Map<string, Link>();
  const used = new Set<string>();
  for (const link of map?.links ?? []) {
    used.add(link.type);
    if (!shown.has(link.src_label) || !shown.has(link.dst_label)) continue;
    const key = `rel|${link.src_label}|${link.dst_label}`;
    const line: Link = byPair.get(key) ?? {
      key,
      src: link.src_label,
      dst: link.dst_label,
      kind: "relationship",
      types: [],
      count: 0,
      caption: "",
    };
    line.types.push({ type: link.type, count: link.count, estimated: link.estimated });
    line.count += link.count;
    byPair.set(key, line);
  }
  for (const line of byPair.values()) {
    line.types.sort((a, b) => b.count - a.count || a.type.localeCompare(b.type));
    line.caption = linkCaption(line.types, text.formatCount, text.more);
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
  return JSON.stringify([diagram.boxes.map((b) => [b.label, b.w, b.h]), diagram.blocks.map(shape), diagram.lone]);
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
export const BLOCK_OPTIONS = packOptions(`[top=${BLOCK_TITLE + 8},left=12,bottom=12,right=12]`);

export const familyId = (root: string) => `family:${root}`;

/** The diagram as an ELK graph: blocks (nested as inheritance nests), then the lone labels. */
export function toElkGraph(diagram: Diagram): ElkNode {
  const size = new Map(diagram.boxes.map((b) => [b.label, b] as const));
  const item = (it: string | Block): ElkNode =>
    typeof it === "string"
      ? { id: it, width: size.get(it)!.w, height: size.get(it)!.h }
      : { id: familyId(it.root), layoutOptions: BLOCK_OPTIONS, children: it.items.map(item) };
  return { id: "schema", layoutOptions: GRID_OPTIONS, children: [...diagram.blocks, ...diagram.lone].map(item) };
}

/** Every box's and block's place out of ELK's answer, in diagram coordinates. */
export function readElkLayout(laid: ElkNode): Layout {
  const layout: Layout = { positions: {}, blocks: {} };
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
  return layout;
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

/**
 * A relationship line (boxes given by centre and size): a gentle curve from
 * border to border, bowed further when its reverse also
 * exists so the two do not overlap; a label's line to itself is a loop above it.
 */
export function freeGeometry(src: Rect, dst: Rect, self: boolean, bowed: boolean): { d: string; caption: Point } {
  if (self) {
    const top = src.y - src.h / 2;
    const left = src.x + src.w / 2 - 34;
    return {
      d: `M ${left} ${top} C ${left - 10} ${top - 46}, ${left + 40} ${top - 46}, ${left + 30} ${top}`,
      caption: { x: left + 10, y: top - 46 },
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
  return {
    d: `M ${a.x} ${a.y} Q ${control.x} ${control.y} ${b.x} ${b.y}`,
    caption: { x: (a.x + 2 * control.x + b.x) / 4, y: (a.y + 2 * control.y + b.y) / 4 },
  };
}

// v2: positions of labels in a block are relative to the block.
const STORAGE_PREFIX = "graphos.schemaDiagram.v2.";

/** Positions the user arranged for this tenant, in this browser (empty when unavailable). */
export function loadPositions(tenant: string): Positions {
  try {
    const raw = window.localStorage.getItem(STORAGE_PREFIX + tenant);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? (parsed as Positions) : {};
  } catch {
    return {};
  }
}

/** Remembers positions in this browser; storage that is unavailable is ignored. */
export function savePositions(tenant: string, positions: Positions): void {
  try {
    if (Object.keys(positions).length === 0) window.localStorage.removeItem(STORAGE_PREFIX + tenant);
    else window.localStorage.setItem(STORAGE_PREFIX + tenant, JSON.stringify(positions));
  } catch {
    /* not kept: the layout is computed again next time */
  }
}

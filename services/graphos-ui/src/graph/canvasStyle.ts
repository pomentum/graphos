import { metricSize } from "../lib/metrics";
import { middleEllipsis } from "../lib/text";
import { rankColors } from "../theme/categories";
import type { Palette } from "../theme/tokens";
import { displayName, type EdgeAttributes, type GraphModel, type NodeAttributes } from "./GraphModel";
import { nameGroups, type GroupName } from "./groups";

/** How nodes that are not emphasised are coloured. */
export type ColorBy = "none" | "label" | "group";

/** What is emphasised on the canvas right now. */
export interface Emphasis {
  selected: string | null;
  /** The selected edge, if an edge rather than a node is selected. */
  selectedEdge?: string | null;
  /** How idle nodes are coloured; grey when absent. */
  categories?: Categories;
  /** Nodes and edges to show in blue: the selection's neighbourhood, or a path. */
  focus: { nodes: Set<string>; edges: Set<string> };
  /** Nodes someone else just changed. */
  recent: Set<string>;
  /** Nodes an expand just added: labelled for a moment so the eye finds them. */
  fresh?: Set<string>;
  /** The best-connected nodes, labelled while nothing is selected. */
  hubs?: Set<string>;
  /** Show only the focus; everything else is hidden rather than dimmed. */
  isolate?: boolean;
  /** Size nodes by this metric property, against its largest value on the canvas. */
  sizes?: { property: string; max: number };
}

export interface NodeDisplay {
  color: string;
  size: number;
  /** The name as drawn on the canvas, long names cut in the middle. */
  label: string;
  /** The whole name, shown on hover. */
  fullLabel?: string;
  forceLabel: boolean;
  zIndex: number;
  hidden?: boolean;
}

export interface EdgeDisplay {
  color: string;
  size: number;
  zIndex: number;
  hidden?: boolean;
}

/** Neighbours of the selection: a lighter step of the accent, readable on both canvases. */
const FOCUS_BLUE = "#7AA3EB";
/** Above this many highlighted nodes their labels would overlap: show them on hover only. */
export const FOCUS_LABEL_LIMIT = 20;
/** Characters of a name drawn on the canvas; longer ones are cut in the middle (full on hover). */
const LABEL_CHARS = 28;
/** How far nodes and edges outside the focus fade into the canvas (0 = not at all, 1 = gone). */
const DIM = 0.72;

/**
 * How one node is drawn. Grey (or its category colour) by default; the
 * selected node in the accent, its neighbours (or a path) in a lighter blue,
 * and while there is a focus everything outside it fades into the canvas
 * (or, isolated, is hidden). Labels are kept few enough to read: the
 * selection always, the focus while it is small, nodes an expand just added,
 * and the hubs while nothing is selected; any other node on hover.
 */
export function nodeDisplay(key: string, node: NodeAttributes, emphasis: Emphasis, palette: Palette): NodeDisplay {
  const display = nodeDisplayUncut(key, node, emphasis, palette);
  return { ...display, label: middleEllipsis(display.label, LABEL_CHARS), fullLabel: display.label };
}

function nodeDisplayUncut(key: string, node: NodeAttributes, emphasis: Emphasis, palette: Palette): NodeDisplay {
  const name = displayName(node.properties, node.id);
  const size =
    emphasis.sizes && node.more === undefined
      ? metricSize(node.properties[emphasis.sizes.property], emphasis.sizes.max)
      : node.size;
  const focused = emphasis.focus.nodes.size > 0;
  const inFocus = emphasis.focus.nodes.has(key);
  if (key === emphasis.selected) {
    return { color: palette.accent, size: size + 3, label: name, forceLabel: true, zIndex: 3 };
  }
  if (focused && !inFocus && emphasis.isolate) {
    return { color: palette.nodeIdle, size, label: name, forceLabel: false, zIndex: 0, hidden: true };
  }
  if (node.more !== undefined) {
    const color = focused && !inFocus ? mix(palette.muted, palette.canvas, DIM) : palette.muted;
    return { color, size: node.size, label: name, forceLabel: !focused || inFocus, zIndex: 1 };
  }
  const fresh = emphasis.fresh?.has(key) ?? false;
  if (inFocus) {
    const fewEnough = emphasis.focus.nodes.size <= FOCUS_LABEL_LIMIT;
    return { color: FOCUS_BLUE, size: size + 1, label: name, forceLabel: fewEnough || fresh, zIndex: 2 };
  }
  if (emphasis.recent.has(key)) {
    return { color: palette.accentText, size: size + 1, label: name, forceLabel: false, zIndex: 1 };
  }
  const idle = idleColor(node, emphasis.categories, palette);
  return {
    color: focused ? mix(idle, palette.canvas, DIM) : idle,
    size,
    label: name,
    forceLabel: !focused && (fresh || (emphasis.hubs?.has(key) ?? false)),
    zIndex: 0,
  };
}

/** Blends `color` toward `into` by `amount` (hex colours; others are returned as they are). */
export function mix(color: string, into: string, amount: number): string {
  const a = parseHex(color);
  const b = parseHex(into);
  if (!a || !b) return color;
  const channel = (i: number) =>
    Math.round(a[i]! + (b[i]! - a[i]!) * amount)
      .toString(16)
      .padStart(2, "0");
  return `#${channel(0)}${channel(1)}${channel(2)}`;
}

function parseHex(color: string): number[] | null {
  const match = /^#([0-9a-f]{6})$/i.exec(color);
  if (!match) return null;
  const hex = match[1]!;
  return [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
}

/** The category a node is coloured by, or null (grey). */
export function nodeCategory(node: NodeAttributes, colorBy: ColorBy): string | null {
  if (colorBy === "label") return node.vertexLabel;
  if (colorBy === "group") {
    const group = node.properties.community_id;
    return typeof group === "number" || typeof group === "string" ? String(group) : null;
  }
  return null;
}

/** The categories on the canvas, most common first, and the colour of each. */
export interface Categories {
  colorBy: ColorBy;
  /** Category and node count, most common first (ties by name). */
  ranked: [string, number][];
  colors: Map<string, string>;
  /** Nodes outside any category (e.g. not in a group). */
  uncategorised: number;
  /** Colouring by group: the coloured groups named after their members. */
  groupNames?: Map<string, GroupName>;
}

/**
 * Sorts the canvas nodes into categories. Colours go by rank, so the most
 * common categories are always told apart; a category keeps its colour while
 * the canvas holds the same nodes.
 */
export function categorize(model: GraphModel, colorBy: ColorBy): Categories {
  const counts = new Map<string, number>();
  let uncategorised = 0;
  if (colorBy !== "none") {
    model.graph.forEachNode((_, node) => {
      if (node.more !== undefined) return;
      const category = nodeCategory(node, colorBy);
      if (category === null) uncategorised += 1;
      else counts.set(category, (counts.get(category) ?? 0) + 1);
    });
  }
  const ranked = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const colors = rankColors(ranked.map(([category]) => category));
  const groupNames = colorBy === "group" ? nameGroups(model, colors.keys()) : undefined;
  return { colorBy, ranked, colors, uncategorised, groupNames };
}

function idleColor(node: NodeAttributes, categories: Categories | undefined, palette: Palette): string {
  if (!categories) return palette.nodeIdle;
  const category = nodeCategory(node, categories.colorBy);
  return (category !== null && categories.colors.get(category)) || palette.nodeIdle;
}

/**
 * How one edge is drawn: the selected edge thick, blue when part of the
 * focus, grey otherwise, and faded (or, isolated, hidden) outside a focus.
 */
export function edgeDisplay(key: string, edge: EdgeAttributes, emphasis: Emphasis, palette: Palette): EdgeDisplay {
  if (key === emphasis.selectedEdge) return { color: palette.accent, size: 4, zIndex: 2 };
  if (emphasis.focus.edges.has(key)) return { color: edge.more ? palette.muted : palette.accent, size: 2, zIndex: 1 };
  const focused = emphasis.focus.nodes.size > 0;
  if (focused && emphasis.isolate) return { color: palette.edgeIdle, size: 1, zIndex: 0, hidden: true };
  return { color: focused ? mix(palette.edgeIdle, palette.canvas, DIM) : palette.edgeIdle, size: 1, zIndex: 0 };
}

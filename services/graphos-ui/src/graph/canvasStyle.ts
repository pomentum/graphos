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
}

export interface NodeDisplay {
  color: string;
  size: number;
  label: string;
  forceLabel: boolean;
  zIndex: number;
}

export interface EdgeDisplay {
  color: string;
  size: number;
  zIndex: number;
}

/** Neighbours of the selection: a lighter step of the accent, readable on both canvases. */
const FOCUS_BLUE = "#7AA3EB";
/** Above this many highlighted nodes their labels would overlap: show them on hover only. */
export const FOCUS_LABEL_LIMIT = 20;

/**
 * How one node is drawn. Grey by default; the selected node in the accent,
 * its neighbours (or a path) in a lighter blue. The selection is always
 * labelled, the highlighted nodes only while there are few of them.
 */
export function nodeDisplay(key: string, node: NodeAttributes, emphasis: Emphasis, palette: Palette): NodeDisplay {
  const name = displayName(node.properties, node.id);
  if (key === emphasis.selected) {
    return { color: palette.accent, size: node.size + 3, label: name, forceLabel: true, zIndex: 3 };
  }
  if (emphasis.focus.nodes.has(key)) {
    const fewEnough = emphasis.focus.nodes.size <= FOCUS_LABEL_LIMIT;
    return { color: FOCUS_BLUE, size: node.size + 1, label: name, forceLabel: fewEnough, zIndex: 2 };
  }
  if (emphasis.recent.has(key)) {
    return { color: palette.accentText, size: node.size + 1, label: name, forceLabel: false, zIndex: 1 };
  }
  return {
    color: idleColor(node, emphasis.categories, palette),
    size: node.size,
    label: name,
    forceLabel: false,
    zIndex: 0,
  };
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

/** How one edge is drawn: the selected edge thick, blue when part of the focus, grey otherwise. */
export function edgeDisplay(key: string, _edge: EdgeAttributes, emphasis: Emphasis, palette: Palette): EdgeDisplay {
  if (key === emphasis.selectedEdge) return { color: palette.accent, size: 4, zIndex: 2 };
  return emphasis.focus.edges.has(key)
    ? { color: palette.accent, size: 2, zIndex: 1 }
    : { color: palette.edgeIdle, size: 1, zIndex: 0 };
}

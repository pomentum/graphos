import type { Palette } from "../theme/tokens";
import { displayName, type EdgeAttributes, type NodeAttributes } from "./GraphModel";

/** What is emphasised on the canvas right now. */
export interface Emphasis {
  selected: string | null;
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
  return { color: palette.nodeIdle, size: node.size, label: name, forceLabel: false, zIndex: 0 };
}

/** How one edge is drawn: blue when it is part of the focus, grey otherwise. */
export function edgeDisplay(key: string, _edge: EdgeAttributes, emphasis: Emphasis, palette: Palette): EdgeDisplay {
  return emphasis.focus.edges.has(key)
    ? { color: palette.accent, size: 2, zIndex: 1 }
    : { color: palette.edgeIdle, size: 1, zIndex: 0 };
}

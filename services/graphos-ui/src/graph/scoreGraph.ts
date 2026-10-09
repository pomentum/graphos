import forceAtlas2 from "graphology-layout-forceatlas2";
import type { InducedEdgesResponse, NodesScanResponse, Properties } from "../api/types";
import { metricSize } from "../lib/metrics";
import { middleEllipsis } from "../lib/text";
import type { Palette } from "../theme/tokens";
import { mix, type Categories, type EdgeDisplay, type Emphasis, type NodeDisplay } from "./canvasStyle";
import { displayName, GraphModel, nodeKey, type EdgeAttributes, type NodeAttributes } from "./GraphModel";

/**
 * The graph through one metric: the nodes a scan returned (the top nodes by
 * a score, or one connected component) and the relationships among them,
 * sized and coloured by the score. Used by the Groups mode's metric views.
 */

export type ScannedNode = NodesScanResponse["items"][number];

/** How many nodes are labelled on the canvas while nothing is selected: the top ones. */
export const SCORE_LABELS = 12;

/**
 * Merges per-label scans into one list. A label's scan also returns the nodes
 * of the labels under it (`hierarchy`: child -> parent), named after itself,
 * so a node seen under a label and under one of its ancestors is kept once,
 * under the more specific label. Sorted by `property`, highest first, when
 * given, and cut to `limit`.
 */
export function mergeScans(
  scans: ScannedNode[][],
  hierarchy: Record<string, string>,
  property: string | null,
  limit: number,
): ScannedNode[] {
  const isAncestor = (maybe: string, of: string) => {
    for (let label = hierarchy[of]; label !== undefined; label = hierarchy[label]) if (label === maybe) return true;
    return false;
  };
  const byId = new Map<string, ScannedNode[]>();
  for (const item of scans.flat()) {
    const seen = byId.get(item.id) ?? [];
    if (seen.some((other) => other.label === item.label || isAncestor(item.label, other.label))) continue;
    byId.set(item.id, [...seen.filter((other) => !isAncestor(other.label, item.label)), item]);
  }
  const merged = [...byId.values()].flat();
  if (property) merged.sort((a, b) => score(b.properties, property) - score(a.properties, property));
  return merged.slice(0, limit);
}

function score(properties: Properties, property: string): number {
  const value = properties[property];
  return typeof value === "number" ? value : -Infinity;
}

/**
 * Each component and its size, largest first, from per-label `graphos_component`
 * histograms. A label's histogram also counts the nodes of the labels under it
 * (`hierarchy`: child -> parent), so each label adds only its own nodes: its
 * counts less those of its direct children.
 */
export function mergeComponentCounts(
  byLabel: Record<string, { value: string; count: number }[]>,
  hierarchy: Record<string, string>,
): { id: number; size: number }[] {
  const counts = (label: string) => {
    const out = new Map<number, number>();
    for (const bin of byLabel[label] ?? []) {
      const id = Number(bin.value);
      if (bin.value !== "" && Number.isFinite(id)) out.set(id, (out.get(id) ?? 0) + bin.count);
    }
    return out;
  };
  const sizes = new Map<number, number>();
  for (const label of Object.keys(byLabel)) {
    const own = counts(label);
    for (const [child, parent] of Object.entries(hierarchy)) {
      if (parent !== label) continue;
      for (const [id, n] of counts(child)) own.set(id, (own.get(id) ?? 0) - n);
    }
    for (const [id, n] of own) if (n > 0) sizes.set(id, (sizes.get(id) ?? 0) + n);
  }
  return [...sizes.entries()].map(([id, size]) => ({ id, size })).sort((a, b) => b.size - a.size || a.id - b.id);
}

/**
 * The canvas model: one node per scanned node, sized by `property` against its
 * largest value (all the same size without one), one edge per relationship
 * among them, laid out with ForceAtlas2.
 */
export function buildScoreModel(
  nodes: ScannedNode[],
  edges: InducedEdgesResponse["edges"],
  property: string | null,
): GraphModel {
  const model = new GraphModel();
  const graph = model.graph;
  const max = property ? Math.max(0, ...nodes.map((n) => score(n.properties, property))) : 0;
  const keyOf = new Map<string, string>();
  nodes.forEach((node, i) => {
    const key = nodeKey({ id: node.id, label: node.label });
    if (graph.hasNode(key)) return;
    if (!keyOf.has(node.id)) keyOf.set(node.id, key);
    const angle = i * 2.399963;
    const r = Math.sqrt(i + 1);
    graph.addNode(key, {
      id: node.id,
      vertexLabel: node.label,
      properties: node.properties,
      size: property ? metricSize(node.properties[property], max) : 6,
      x: r * Math.cos(angle),
      y: r * Math.sin(angle),
    });
  });
  for (const edge of edges) {
    const [src, dst] = [keyOf.get(edge.src_id), keyOf.get(edge.dst_id)];
    if (!src || !dst || src === dst) continue;
    const key = `${src}|${edge.type}|${dst}`;
    if (graph.hasEdge(key)) continue;
    graph.addDirectedEdgeWithKey(key, src, dst, { edgeType: edge.type, properties: edge.properties });
  }
  if (graph.order > 2) {
    forceAtlas2.assign(graph, {
      iterations: 300,
      settings: { ...forceAtlas2.inferSettings(graph), adjustSizes: true, scalingRatio: 20, gravity: 1 },
    });
  }
  return model;
}

/** Heat colours, coolest to hottest, for ranks from last to first. */
const HEAT = ["#4C6EF5", "#7950F2", "#E64980", "#FD7E14", "#FAB005"];

/** A colour from the heat ramp for `t` in 0 (low) to 1 (high). */
export function heatColor(t: number): string {
  const x = Math.min(Math.max(t, 0), 1) * (HEAT.length - 1);
  const i = Math.min(Math.floor(x), HEAT.length - 2);
  return mix(HEAT[i]!, HEAT[i + 1]!, x - i);
}

/** Colours by rank: the first node hottest, the last coolest. */
export function rankHeat(keys: string[]): Categories {
  const colors = new Map<string, string>();
  keys.forEach((key, i) => colors.set(key, heatColor(keys.length > 1 ? 1 - i / (keys.length - 1) : 1)));
  return { colorBy: "group", ranked: keys.map((key) => [key, 1]), colors, uncategorised: 0 };
}

/** How a node is drawn in a metric view: its heat colour, labelled when among the top ones. */
export function scoreNodeDisplay(key: string, node: NodeAttributes, emphasis: Emphasis, palette: Palette): NodeDisplay {
  const name = displayName(node.properties, node.id);
  const label = middleEllipsis(name, 28);
  const colour = emphasis.categories?.colors.get(key) ?? palette.nodeIdle;
  if (key === emphasis.selected) {
    return { color: palette.accent, size: node.size + 2, label, fullLabel: name, forceLabel: true, zIndex: 3 };
  }
  const focused = emphasis.focus.nodes.size > 0;
  const faded = focused && !emphasis.focus.nodes.has(key);
  return {
    color: faded ? mix(colour, palette.canvas, 0.75) : colour,
    size: node.size,
    label,
    fullLabel: name,
    forceLabel: focused ? !faded && emphasis.focus.nodes.size <= SCORE_LABELS : (emphasis.hubs?.has(key) ?? false),
    zIndex: faded ? 0 : 1,
  };
}

/** How a relationship is drawn: thin, in the accent when it touches the selection. */
export function scoreEdgeDisplay(
  key: string,
  _edge: EdgeAttributes,
  emphasis: Emphasis,
  palette: Palette,
): EdgeDisplay {
  if (emphasis.focus.edges.has(key)) return { color: palette.accent, size: 1.5, zIndex: 1 };
  const focused = emphasis.focus.nodes.size > 0;
  return { color: focused ? mix(palette.edgeIdle, palette.canvas, 0.75) : palette.edgeIdle, size: 0.8, zIndex: 0 };
}

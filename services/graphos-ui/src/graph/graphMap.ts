import forceAtlas2 from "graphology-layout-forceatlas2";
import type { GraphMap } from "../api/types";
import { t } from "../i18n";
import { formatCount } from "../lib/time";
import { rankColors } from "../theme/categories";
import type { Palette } from "../theme/tokens";
import { mix, type Categories, type EdgeDisplay, type Emphasis, type NodeDisplay } from "./canvasStyle";
import { GraphModel, type EdgeAttributes, type NodeAttributes } from "./GraphModel";

/** One relationship type between two labels, with its edge count. */
export interface MapLinkCount {
  type: string;
  count: number;
}

/** What one label is linked to: outgoing and incoming types per other label, and links to itself. */
export interface LabelLinks {
  outgoing: { label: string; type: string; count: number }[];
  incoming: { label: string; type: string; count: number }[];
}

/**
 * The map as a graph: one node per label (`properties.count` its nodes),
 * one edge per pair of labels with edges between them (`properties.count`
 * all of them, `properties.types` per type). Links of a label to itself are
 * not drawn; the label's panel lists them. Laid out with ForceAtlas2,
 * stronger links pulling harder, bubbles kept apart by their size.
 */
export function buildMapModel(map: GraphMap): GraphModel {
  const model = new GraphModel();
  const graph = model.graph;
  map.labels.forEach((entry, i) => {
    const angle = i * 2.399963;
    const r = Math.sqrt(i + 1);
    graph.addNode(entry.label, {
      id: entry.label,
      vertexLabel: entry.label,
      properties: { name: entry.label, count: entry.count },
      size: bubbleSize(entry.count),
      x: r * Math.cos(angle),
      y: r * Math.sin(angle),
    });
  });
  for (const link of map.links) {
    if (link.src_label === link.dst_label || !graph.hasNode(link.src_label) || !graph.hasNode(link.dst_label)) {
      continue;
    }
    const key = `${link.src_label}|${link.dst_label}`;
    if (!graph.hasEdge(key)) {
      graph.addDirectedEdgeWithKey(key, link.src_label, link.dst_label, {
        edgeType: link.type,
        properties: { count: 0, types: [] as MapLinkCount[] },
      });
    }
    const properties = graph.getEdgeAttribute(key, "properties") as { count: number; types: MapLinkCount[] };
    properties.count += link.count;
    properties.types.push({ type: link.type, count: link.count });
  }
  keepStrongestLinks(model);
  if (graph.order > 2) {
    forceAtlas2.assign(graph, {
      iterations: 500,
      getEdgeWeight: (_edge, attributes) => Math.log10(Number(attributes.properties.count) + 1),
      settings: { ...forceAtlas2.inferSettings(graph), adjustSizes: true, scalingRatio: 60, gravity: 0.4 },
    });
  }
  return model;
}

/** Bubbles labelled while nothing is selected: the largest ones. */
export const MAP_LABELS = 20;
/** A selection's neighbours are labelled while there are at most this many. */
const MAP_FOCUS_LABELS = 24;

/** The keys of the `count` largest bubbles, to label. */
export function largestBubbles(model: GraphModel, count = MAP_LABELS): Set<string> {
  const sized = model.graph.mapNodes((key, node) => [key, Number(node.properties.count ?? 0)] as const);
  return new Set(
    [...sized]
      .sort((a, b) => b[1] - a[1])
      .slice(0, count)
      .map(([key]) => key),
  );
}

/** Lines kept per bubble on the map and the groups: its strongest links. */
export const LINKS_PER_BUBBLE = 4;

/**
 * Keeps each bubble's `perBubble` strongest lines (by edge count) and drops
 * the rest, so the drawing shows structure instead of a hairball: a line
 * stays if it is among the strongest of either of its ends. The panels still
 * list every link; only the drawing is thinned.
 */
export function keepStrongestLinks(model: GraphModel, perBubble = LINKS_PER_BUBBLE): void {
  const graph = model.graph;
  const keep = new Set<string>();
  graph.forEachNode((key) => {
    graph
      .edges(key)
      .map((edge) => [edge, Number(graph.getEdgeAttribute(edge, "properties").count ?? 0)] as const)
      .sort((a, b) => b[1] - a[1])
      .slice(0, perBubble)
      .forEach(([edge]) => keep.add(edge));
  });
  for (const edge of graph.edges()) if (!keep.has(edge)) graph.dropEdge(edge);
}

/** Bubble size by node count: logarithmic, so a label of a few nodes stays visible beside one of millions. */
export function bubbleSize(count: number): number {
  return 5 + 3.2 * Math.log10(count + 1);
}

/** The labels' colours: by size rank, as the canvas colours labels. */
export function mapCategories(map: GraphMap): Categories {
  const ranked = map.labels.map((l) => [l.label, l.count] as [string, number]);
  return { colorBy: "label", ranked, colors: rankColors(ranked.map(([label]) => label)), uncategorised: 0 };
}

/** What `label` links to, most edges first. */
export function linksOf(map: GraphMap, label: string): LabelLinks {
  const byCount = (a: { count: number }, b: { count: number }) => b.count - a.count;
  return {
    outgoing: map.links
      .filter((l) => l.src_label === label)
      .map((l) => ({ label: l.dst_label, type: l.type, count: l.count }))
      .sort(byCount),
    incoming: map.links
      .filter((l) => l.dst_label === label && l.src_label !== label)
      .map((l) => ({ label: l.src_label, type: l.type, count: l.count }))
      .sort(byCount),
  };
}

/**
 * How a label bubble is drawn: its colour, its size, always labelled with
 * its count; with a selection, the selected label in the accent, the labels
 * it links to in full colour and the rest faded.
 */
export function mapNodeDisplay(key: string, node: NodeAttributes, emphasis: Emphasis, palette: Palette): NodeDisplay {
  const count = Number(node.properties.count ?? 0);
  const name = typeof node.properties.name === "string" ? node.properties.name : node.vertexLabel;
  const label = t("views.explore.map.bubbleLabel", { label: name, formatted: formatCount(count) });
  // The whole name (a group's hub may be a long path) on hover; a short one on the canvas.
  const full = typeof node.properties.fullName === "string" ? node.properties.fullName : name;
  const fullLabel = t("views.explore.map.bubbleLabel", { label: full, formatted: formatCount(count) });
  // Colours are keyed by the bubble's key: the label on the map, the group in Groups.
  const colour = emphasis.categories?.colors.get(key) ?? palette.nodeIdle;
  const focused = emphasis.focus.nodes.size > 0;
  if (key === emphasis.selected) {
    return { color: palette.accent, size: node.size + 2, label, fullLabel, forceLabel: true, zIndex: 3 };
  }
  const faded = focused && !emphasis.focus.nodes.has(key);
  // Labelled: the largest bubbles (emphasis.hubs) while nothing is selected, else
  // the selection's neighbours while they are few; any other bubble on hover.
  const labelled = focused
    ? !faded && emphasis.focus.nodes.size <= MAP_FOCUS_LABELS
    : (emphasis.hubs?.has(key) ?? true);
  return {
    color: faded ? mix(colour, palette.canvas, 0.75) : colour,
    size: node.size,
    label,
    fullLabel,
    forceLabel: labelled,
    zIndex: faded ? 0 : 1,
  };
}

/** How a link is drawn: thicker with more edges; in the accent when it touches the selection. */
export function mapEdgeDisplay(key: string, edge: EdgeAttributes, emphasis: Emphasis, palette: Palette): EdgeDisplay {
  const size = 0.6 + 0.9 * Math.log10(Number(edge.properties.count ?? 0) + 1);
  if (emphasis.focus.edges.has(key)) return { color: palette.accent, size, zIndex: 1 };
  const focused = emphasis.focus.nodes.size > 0;
  return { color: focused ? mix(palette.edgeIdle, palette.canvas, 0.75) : palette.edgeIdle, size, zIndex: 0 };
}

import forceAtlas2 from "graphology-layout-forceatlas2";
import type { GraphMap } from "../api/types";
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
  if (graph.order > 2) {
    forceAtlas2.assign(graph, {
      iterations: 500,
      getEdgeWeight: (_edge, attributes) => Math.log10(Number(attributes.properties.count) + 1),
      settings: { ...forceAtlas2.inferSettings(graph), adjustSizes: true, scalingRatio: 60, gravity: 0.4 },
    });
  }
  return model;
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
  const label = `${node.vertexLabel} · ${formatCount(count)}`;
  const colour = emphasis.categories?.colors.get(node.vertexLabel) ?? palette.nodeIdle;
  const focused = emphasis.focus.nodes.size > 0;
  if (key === emphasis.selected) {
    return { color: palette.accent, size: node.size + 2, label, fullLabel: label, forceLabel: true, zIndex: 3 };
  }
  const faded = focused && !emphasis.focus.nodes.has(key);
  return {
    color: faded ? mix(colour, palette.canvas, 0.75) : colour,
    size: node.size,
    label,
    fullLabel: label,
    forceLabel: !faded,
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

import { toBlob } from "@sigma/export-image";
import type Graph from "graphology";
import type Sigma from "sigma";
import type { EdgeAttributes, NodeAttributes } from "../graph/GraphModel";
import type { Palette } from "../theme/tokens";

/**
 * What is on a graph canvas, as a file: a picture (PNG, drawn by sigma; SVG,
 * drawn from sigma's own positions, colours and labels, so it opens in other
 * tools) or the graph itself (JSON). Made in the browser from what the canvas
 * holds; nothing goes to graphos.
 */

type Renderer = Sigma<NodeAttributes, EdgeAttributes>;

/** The canvas as a PNG, on its background, at the canvas's size and the screen's pixel density. */
export function canvasPng(renderer: Renderer, palette: Palette): Promise<Blob> {
  return toBlob(renderer as unknown as Sigma, { format: "png", backgroundColor: palette.canvas });
}

function escapeXml(text: string): string {
  return text.replace(
    /[<>&"']/g,
    (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;" })[c]!,
  );
}

const round = (n: number) => Math.round(n * 10) / 10;

/**
 * The canvas as SVG, as it is drawn now: edges (with arrowheads), nodes, and
 * the labels sigma shows (those of highlighted and forced nodes), on the
 * canvas's background and at its size. Hidden nodes and edges are left out.
 */
export function canvasSvg(renderer: Renderer, graph: Graph<NodeAttributes, EdgeAttributes>, palette: Palette): string {
  const { width, height } = renderer.getDimensions();
  const at = (key: string) => {
    const data = renderer.getNodeDisplayData(key);
    if (!data || data.hidden) return null;
    const p = renderer.framedGraphToViewport({ x: data.x, y: data.y });
    return { x: p.x, y: p.y, r: Math.max(renderer.scaleSize(data.size), 1), data };
  };
  const markers = new Map<string, string>();
  const markerFor = (color: string) => {
    let id = markers.get(color);
    if (!id) {
      id = `a${markers.size}`;
      markers.set(color, id);
    }
    return id;
  };

  const edges: string[] = [];
  graph.forEachEdge((key, _attributes, source, target) => {
    const data = renderer.getEdgeDisplayData(key);
    const a = at(source);
    const b = at(target);
    if (!data || data.hidden || !a || !b || source === target) return;
    // From border to border, so the arrowhead is not under the target.
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const length = Math.hypot(dx, dy) || 1;
    const x1 = a.x + (dx / length) * a.r;
    const y1 = a.y + (dy / length) * a.r;
    const x2 = b.x - (dx / length) * b.r;
    const y2 = b.y - (dy / length) * b.r;
    const stroke = Math.max(renderer.scaleSize(data.size), 0.5);
    edges.push(
      `<line x1="${round(x1)}" y1="${round(y1)}" x2="${round(x2)}" y2="${round(y2)}" stroke="${data.color}" ` +
        `stroke-width="${round(stroke)}" marker-end="url(#${markerFor(data.color)})"/>`,
    );
  });

  const nodes: string[] = [];
  const labels: string[] = [];
  const order: { key: string; z: number }[] = [];
  graph.forEachNode((key) => order.push({ key, z: renderer.getNodeDisplayData(key)?.zIndex ?? 0 }));
  for (const { key } of order.sort((p, q) => p.z - q.z)) {
    const node = at(key);
    if (!node) continue;
    nodes.push(`<circle cx="${round(node.x)}" cy="${round(node.y)}" r="${round(node.r)}" fill="${node.data.color}"/>`);
    if (node.data.label && (node.data.forceLabel || node.data.highlighted)) {
      labels.push(
        `<text x="${round(node.x + node.r + 3)}" y="${round(node.y + 4)}">${escapeXml(node.data.label)}</text>`,
      );
    }
  }

  const defs = [...markers]
    .map(
      ([color, id]) =>
        `<marker id="${id}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" ` +
        `orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="${color}"/></marker>`,
    )
    .join("");
  return [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`,
    `<defs>${defs}</defs>`,
    `<rect width="100%" height="100%" fill="${palette.canvas}"/>`,
    `<g>${edges.join("")}</g>`,
    `<g>${nodes.join("")}</g>`,
    `<g font-family="-apple-system, BlinkMacSystemFont, Inter, sans-serif" font-size="11" font-weight="500" ` +
      `fill="${palette.text}">${labels.join("")}</g>`,
    "</svg>",
  ].join("");
}

/**
 * The graph on the canvas as JSON: its nodes (graphos id, label, properties)
 * and edges (ends and type, properties); "+N more" placeholders left out.
 */
export function canvasJson(graph: Graph<NodeAttributes, EdgeAttributes>): string {
  const nodes: { id: string; label: string; properties: unknown }[] = [];
  const refs = new Map<string, { id: string; label: string }>();
  graph.forEachNode((key, node) => {
    if (node.more !== undefined) return;
    refs.set(key, { id: node.id, label: node.vertexLabel });
    nodes.push({ id: node.id, label: node.vertexLabel, properties: node.properties });
  });
  const edges: unknown[] = [];
  graph.forEachEdge((_key, edge, source, target) => {
    const src = refs.get(source);
    const dst = refs.get(target);
    if (edge.more || !src || !dst) return;
    edges.push({
      src_id: src.id,
      src_label: src.label,
      dst_id: dst.id,
      dst_label: dst.label,
      type: edge.edgeType,
      properties: edge.properties,
    });
  });
  return JSON.stringify({ nodes, edges }, null, 2);
}

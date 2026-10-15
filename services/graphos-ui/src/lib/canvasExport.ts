import { toBlob } from "@sigma/export-image";
import type Graph from "graphology";
import type Sigma from "sigma";
import type { EdgeAttributes, NodeAttributes } from "../graph/GraphModel";
import type { Palette } from "../theme/tokens";
import { SVG_FONT_FAMILY } from "./svgFont";

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

/** The direction of (x, y), of length 1 ((0, 0) stays put). */
function unit(x: number, y: number): [number, number] {
  const length = Math.hypot(x, y) || 1;
  return [x / length, y / length];
}

/** Whether sigma draws the node's label: a highlighted or forced node's. */
function labelShown(data: { label?: string | null; forceLabel?: boolean; highlighted?: boolean }): boolean {
  return Boolean(data.label && (data.forceLabel || data.highlighted));
}

/** The text of the labels the canvas shows now, for picking the font to embed. */
export function canvasLabelText(renderer: Renderer, graph: Graph<NodeAttributes, EdgeAttributes>): string {
  const text: string[] = [];
  graph.forEachNode((key) => {
    const data = renderer.getNodeDisplayData(key);
    if (data && !data.hidden && labelShown(data)) text.push(data.label!);
  });
  return text.join("");
}

/** A pictogram's image (drawn in white) in the colour the canvas tints it with. */
function tinted(image: string, color: string): string {
  return image.replaceAll(encodeURIComponent("#FFFFFF"), encodeURIComponent(color));
}

// The image program's padding (GraphCanvas): the pictogram spans the disc's
// diameter divided by 1 + 2 × padding.
const PICTOGRAM_SCALE = 1 / (1 + 2 * 0.24);

/**
 * The canvas as SVG, as it is drawn now: edges (with arrowheads; those sharing
 * their two nodes curved apart, as on the canvas), nodes with their label's
 * pictogram, and the labels sigma shows (those of highlighted and forced
 * nodes), on the canvas's background and at its size. Hidden nodes and edges
 * are left out. `fontFaces` (from `svgFont`) embeds the labels' font.
 */
export function canvasSvg(
  renderer: Renderer,
  graph: Graph<NodeAttributes, EdgeAttributes>,
  palette: Palette,
  fontFaces = "",
): string {
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
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const stroke = Math.max(renderer.scaleSize(data.size), 0.5);
    const paint = `stroke="${data.color}" stroke-width="${round(stroke)}" marker-end="url(#${markerFor(data.color)})"`;
    // Edges sharing their two nodes are drawn curved apart (graph/parallelEdges):
    // the same quadratic as sigma's curve program, its control point off the
    // middle by curvature × the edge's length, on the screen's axes (y down).
    const curvature = (data as { curvature?: unknown }).curvature;
    if (typeof curvature === "number" && curvature !== 0) {
      const cx = (a.x + b.x) / 2 + dy * curvature;
      const cy = (a.y + b.y) / 2 - dx * curvature;
      // From border to border along the curve's own ends, so the arrowhead is not under the target.
      const [sx, sy] = unit(cx - a.x, cy - a.y);
      const [ex, ey] = unit(b.x - cx, b.y - cy);
      edges.push(
        `<path d="M${round(a.x + sx * a.r)},${round(a.y + sy * a.r)} Q${round(cx)},${round(cy)} ` +
          `${round(b.x - ex * b.r)},${round(b.y - ey * b.r)}" fill="none" ${paint}/>`,
      );
      return;
    }
    // From border to border, so the arrowhead is not under the target.
    const [ux, uy] = unit(dx, dy);
    edges.push(
      `<line x1="${round(a.x + ux * a.r)}" y1="${round(a.y + uy * a.r)}" x2="${round(b.x - ux * b.r)}" ` +
        `y2="${round(b.y - uy * b.r)}" ${paint}/>`,
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
    const { image, pictoColor } = node.data as { image?: string; pictoColor?: string };
    if (image) {
      const side = 2 * node.r * PICTOGRAM_SCALE;
      nodes.push(
        `<image xlink:href="${escapeXml(tinted(image, pictoColor ?? "#FFFFFF"))}" x="${round(node.x - side / 2)}" ` +
          `y="${round(node.y - side / 2)}" width="${round(side)}" height="${round(side)}"/>`,
      );
    }
    if (labelShown(node.data)) {
      labels.push(
        `<text x="${round(node.x + node.r + 3)}" y="${round(node.y + 4)}">${escapeXml(node.data.label!)}</text>`,
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
    `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`,
    `<defs>${fontFaces ? `<style>${fontFaces}</style>` : ""}${defs}</defs>`,
    `<rect width="100%" height="100%" fill="${palette.canvas}"/>`,
    `<g>${edges.join("")}</g>`,
    `<g>${nodes.join("")}</g>`,
    `<g font-family="${SVG_FONT_FAMILY}" font-size="11" font-weight="500" ` +
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

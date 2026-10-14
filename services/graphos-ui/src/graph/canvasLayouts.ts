import type { ELK as ElkEngine, ElkNode } from "elkjs/lib/elk-api";
import type { GraphModel } from "./GraphModel";
import { settle, stopSettling } from "./layout";

/** How the Graph canvas places its nodes. */
export const LAYOUTS = ["force", "circular", "layered"] as const;
export type CanvasLayout = (typeof LAYOUTS)[number];

/** Above this many nodes a layered drawing grows too wide to read and too slow to compute. */
export const LAYERED_MAX_NODES = 500;

/** Room per node on the circle, in graph units (sigma frames the result). */
const CIRCLE_STEP = 12;

/**
 * Nodes on a circle, label by label (most connected first within a label),
 * so each label is one arc and edges between labels cross the middle.
 * "+N more" placeholders stay beside their node.
 */
export function circular(model: GraphModel): void {
  stopSettling();
  const graph = model.graph;
  const nodes = graph
    .filterNodes((_, node) => node.more === undefined)
    .sort((a, b) => {
      const la = graph.getNodeAttribute(a, "vertexLabel");
      const lb = graph.getNodeAttribute(b, "vertexLabel");
      return la.localeCompare(lb) || graph.degree(b) - graph.degree(a) || a.localeCompare(b);
    });
  const radius = Math.max(60, (nodes.length * CIRCLE_STEP) / (2 * Math.PI));
  nodes.forEach((key, i) => {
    const angle = (2 * Math.PI * i) / nodes.length - Math.PI / 2;
    graph.mergeNodeAttributes(key, { x: radius * Math.cos(angle), y: radius * Math.sin(angle) });
  });
  placeMore(model, 1.15);
}

/**
 * Nodes in layers along their edges, left to right (ELK's layered
 * algorithm): reads well for paths, expansions and chains of dependencies.
 * Only up to `LAYERED_MAX_NODES`; resolves false when the canvas is larger.
 */
export async function layered(model: GraphModel, using?: Pick<ElkEngine, "layout">): Promise<boolean> {
  const graph = model.graph;
  const nodes = graph.filterNodes((_, node) => node.more === undefined);
  if (nodes.length > LAYERED_MAX_NODES) return false;
  stopSettling();
  const kept = new Set(nodes);
  const elkGraph: ElkNode = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.layered.spacing.nodeNodeBetweenLayers": "60",
      "elk.spacing.nodeNode": "18",
      "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
      "elk.separateConnectedComponents": "true",
    },
    children: nodes.map((id) => ({ id, width: 12, height: 12 })),
    edges: graph
      .filterEdges((_, __, source, target) => kept.has(source) && kept.has(target) && source !== target)
      .map((id) => ({ id, sources: [graph.source(id)], targets: [graph.target(id)] })),
  };
  const engine = using ?? (await elk());
  const result = await engine.layout(elkGraph);
  for (const child of result.children ?? []) {
    // ELK's y grows downwards; sigma's upwards.
    if (graph.hasNode(child.id)) graph.mergeNodeAttributes(child.id, { x: child.x ?? 0, y: -(child.y ?? 0) });
  }
  placeMore(model, 1);
  return true;
}

/** ForceAtlas2 again, from where the nodes are now (stops by itself). */
export function force(model: GraphModel): () => void {
  return settle(model);
}

/** A "+N more" placeholder just outside its node, away from the centre. */
function placeMore(model: GraphModel, outward: number): void {
  const graph = model.graph;
  graph.forEachNode((key, node) => {
    if (node.more === undefined) return;
    const center = graph.neighbors(key)[0];
    if (!center) return;
    const { x, y } = graph.getNodeAttributes(center);
    const length = Math.hypot(x, y) || 1;
    graph.mergeNodeAttributes(key, { x: x * outward + (x / length) * 20, y: y * outward + (y / length) * 20 });
  });
}

let engine: Promise<ElkEngine> | null = null;

/** ELK in a web worker, loaded with the first layered layout (as the schema diagram does). */
function elk(): Promise<ElkEngine> {
  engine ??= Promise.all([import("elkjs/lib/elk-api"), import("elkjs/lib/elk-worker.min.js?url")]).then(
    ([{ default: ELK }, { default: workerUrl }]) => new ELK({ workerUrl }),
  );
  return engine;
}

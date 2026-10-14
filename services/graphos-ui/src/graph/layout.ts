import forceAtlas2 from "graphology-layout-forceatlas2";
import FA2Layout from "graphology-layout-forceatlas2/worker";
import type { GraphModel } from "./GraphModel";

/** How long the layout runs: longer for bigger graphs, never more than 6 s. */
export function layoutDuration(nodes: number): number {
  return Math.min(8_000, 3_000 + nodes * 0.8);
}

/**
 * Lets a freshly loaded graph settle with ForceAtlas2 in a web worker, so the
 * page stays responsive and the graph visibly untangles. Stops by itself after
 * `layoutDuration`; returns a function that stops it early. Without workers
 * (tests), a short synchronous layout runs instead.
 */
export function settle(model: GraphModel): () => void {
  stopSettling();
  const graph = model.graph;
  if (graph.order < 3) return () => undefined;
  // Strong gravity keeps unconnected nodes near the rest, so they do not
  // stretch the view and squeeze the connected part; a larger scaling ratio
  // gives every node room.
  const settings = {
    ...forceAtlas2.inferSettings(graph),
    barnesHutOptimize: graph.order > 300,
    strongGravityMode: true,
    gravity: 0.6,
    scalingRatio: 12,
    slowDown: 3,
  };
  if (typeof Worker === "undefined") {
    model.relax(60);
    return () => undefined;
  }
  const layout = new FA2Layout(graph, { settings });
  layout.start();
  const timer = setTimeout(() => layout.kill(), layoutDuration(graph.order));
  const stop = () => {
    clearTimeout(timer);
    layout.kill();
    if (running === stop) running = null;
  };
  running = stop;
  return stop;
}

/** The ForceAtlas2 run still moving nodes, if any. */
let running: (() => void) | null = null;

/** Stops a ForceAtlas2 run still under way, e.g. before another layout places the nodes. */
export function stopSettling(): void {
  running?.();
}

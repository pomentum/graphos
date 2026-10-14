import type Graph from "graphology";

/** How far apart parallel edges bend, as a share of their length. */
const CURVE_STEP = 0.35;

/**
 * How much an edge bends so that it and the other edges between the same two
 * nodes (other types, or the other direction) are all visible and clickable:
 * null when it is the only one, or the middle one of an odd number (straight).
 * Each pair's edges are spread symmetrically, by key so the order is stable; an
 * edge running the other way has its sign flipped, as the curve is drawn
 * relative to the edge's own direction.
 */
export function parallelCurvature(graph: Graph, key: string): number | null {
  const source = graph.source(key);
  const target = graph.target(key);
  if (source === target) return null;
  const [a, b] = source < target ? [source, target] : [target, source];
  // (graphology's edges(a, b) may answer both directions: each edge once.)
  const all = [...new Set([...graph.edges(a, b), ...graph.edges(b, a)])].sort();
  if (all.length < 2) return null;
  const offset = CURVE_STEP * (all.indexOf(key) - (all.length - 1) / 2);
  if (offset === 0) return null;
  return source === a ? offset : -offset;
}

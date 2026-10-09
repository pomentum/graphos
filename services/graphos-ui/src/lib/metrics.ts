/**
 * The graph metrics graphos computes and stores on the nodes (`graphos_*`
 * properties, written only by graphos): how they are named, shown, and used
 * to size the nodes on the canvas.
 */

/** The metrics a run can compute; the first three are computed unless chosen otherwise. */
export const METRICS = ["degree", "pagerank", "components", "betweenness"] as const;
export type Metric = (typeof METRICS)[number];
export const DEFAULT_METRICS: Metric[] = ["degree", "pagerank", "components"];

/** What the canvas sizes nodes by: their connections on the canvas, or a stored metric. */
export const SIZE_MODES = ["connections", "graphos_pagerank", "graphos_betweenness", "graphos_degree"] as const;
export type SizeBy = (typeof SIZE_MODES)[number];

/** What Explore's Groups mode shows: the groups, the nodes ranked by a metric, or the components. */
export const LENSES = ["groups", "graphos_pagerank", "graphos_betweenness", "graphos_degree", "components"] as const;
export type Lens = (typeof LENSES)[number];

/** The metric properties a node may carry, in the order they are shown. */
export const METRIC_PROPERTIES = [
  "graphos_pagerank",
  "graphos_betweenness",
  "graphos_degree",
  "graphos_in_degree",
  "graphos_out_degree",
  "graphos_component",
] as const;

/** The smallest and largest node drawn when sizing by a metric. */
const MIN_SIZE = 3;
const MAX_SIZE = 16;

/**
 * A node's size for its share of the largest value on the canvas. The square
 * root keeps a few very large scores from shrinking everything else to dots;
 * nodes without a value are drawn smallest.
 */
export function metricSize(value: unknown, max: number): number {
  if (typeof value !== "number" || !(max > 0) || value <= 0) return MIN_SIZE;
  return MIN_SIZE + (MAX_SIZE - MIN_SIZE) * Math.sqrt(Math.min(value / max, 1));
}

/** The largest value of `property` among `values`, or 0 when none has one. */
export function largestValue(values: Iterable<unknown>): number {
  let max = 0;
  for (const value of values) if (typeof value === "number" && value > max) max = value;
  return max;
}

/** A metric value for reading: scores to four significant digits, counts as they are. */
export function formatMetric(value: unknown): string {
  if (typeof value !== "number") return "";
  if (Number.isInteger(value)) return String(value);
  return value === 0 ? "0" : Number(value.toPrecision(4)).toString();
}

/** Whether a node carries any graph metric. */
export function hasMetrics(properties: Record<string, unknown>): boolean {
  return METRIC_PROPERTIES.some((key) => typeof properties[key] === "number");
}

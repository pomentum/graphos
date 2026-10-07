import type { Neighbor } from "../api/types";

/** Neighbours fetched per expand; graphos allows up to 500. */
export const EXPAND_FETCH_LIMIT = 200;
/** Above this many new nodes, the user picks edge types first. */
export const EXPAND_WARN_AT = 40;

/** Neighbours of one edge type, by direction. */
export interface EdgeTypeCount {
  type: string;
  out: number;
  in: number;
}

/** Counts neighbours per edge type, most common first. */
export function countByEdgeType(neighbors: Neighbor[]): EdgeTypeCount[] {
  const counts = new Map<string, EdgeTypeCount>();
  for (const n of neighbors) {
    const entry = counts.get(n.edge_type) ?? { type: n.edge_type, out: 0, in: 0 };
    entry[n.direction] += 1;
    counts.set(n.edge_type, entry);
  }
  return [...counts.values()].sort((a, b) => b.out + b.in - (a.out + a.in) || a.type.localeCompare(b.type));
}

/** True when adding these neighbours needs the user to narrow them first. */
export function needsChoice(neighbors: Neighbor[], truncated: boolean): boolean {
  return truncated || neighbors.length > EXPAND_WARN_AT;
}

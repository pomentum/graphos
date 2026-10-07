import type { Neighbor } from "../api/types";

/** Neighbours fetched per expand; graphos allows up to 500. */
export const EXPAND_FETCH_LIMIT = 200;

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

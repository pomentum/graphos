import type { GraphMap, TenantSchema } from "../api/types";

/** A label pair an edge type is declared to join. */
export interface Pair {
  src: string;
  dst: string;
}

/**
 * How a (type, source label, target label) stands against the declarations:
 * - `declared`: a declared pair covers it (itself, or one of its labels' parents);
 * - `undeclared`: the type declares pairs, none covers it (refused when strict);
 * - `open`: the type declares no pairs, so any pair is accepted.
 */
export type EndpointStatus = "declared" | "undeclared" | "open";

export interface Coverage {
  status: EndpointStatus;
  /** The declared pair that covers it, when declared. */
  via: Pair | null;
}

/** The labels a label stands for: itself, then its parents (cycle-safe). */
export function lineage(label: string, hierarchy: Record<string, string>): string[] {
  const chain = [label];
  for (let parent = hierarchy[label]; parent && !chain.includes(parent); parent = hierarchy[parent]) {
    chain.push(parent);
  }
  return chain;
}

/** A tenant's declared pairs, to check label pairs against as graphos does. */
export class Endpoints {
  readonly declared: Map<string, Pair[]>;
  private readonly hierarchy: Record<string, string>;

  constructor(schema: Pick<TenantSchema, "edge_endpoints" | "label_hierarchy">) {
    this.declared = new Map(Object.entries(schema.edge_endpoints ?? {}));
    this.hierarchy = schema.label_hierarchy;
  }

  /** Whether any type declares pairs. */
  get any(): boolean {
    return [...this.declared.values()].some((pairs) => pairs.length > 0);
  }

  check(type: string, src: string, dst: string): Coverage {
    const pairs = this.declared.get(type);
    if (!pairs?.length) return { status: "open", via: null };
    const srcs = lineage(src, this.hierarchy);
    const dsts = lineage(dst, this.hierarchy);
    const via = pairs.find((p) => srcs.includes(p.src) && dsts.includes(p.dst)) ?? null;
    return { status: via ? "declared" : "undeclared", via };
  }

  /** Whether an edge of the type may start at a node of this label: open, or a declared pair covers it. */
  startsFrom(type: string, src: string): boolean {
    const pairs = this.declared.get(type);
    if (!pairs?.length) return true;
    const srcs = lineage(src, this.hierarchy);
    return pairs.some((p) => srcs.includes(p.src));
  }

  /** Whether exactly this pair is declared (not only through a parent): it can be removed. */
  exact(type: string, src: string, dst: string): boolean {
    return (this.declared.get(type) ?? []).some((p) => p.src === src && p.dst === dst);
  }
}

/** Label pairs in the data outside the declarations: what strict mode would refuse. */
export function undeclaredInData(
  schema: TenantSchema,
  map: GraphMap | undefined,
): { type: string; src: string; dst: string; count: number }[] {
  const endpoints = new Endpoints(schema);
  return (map?.links ?? [])
    .filter((l) => endpoints.check(l.type, l.src_label, l.dst_label).status === "undeclared")
    .map((l) => ({ type: l.type, src: l.src_label, dst: l.dst_label, count: l.count }))
    .sort((a, b) => b.count - a.count);
}

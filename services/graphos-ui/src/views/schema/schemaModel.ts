import type { GraphMap, TenantSchema } from "../../api/types";
import { Endpoints } from "./endpoints";

/** A vertex label in the schema tree: its depth under its parents and its own node count. */
export interface LabelRow {
  label: string;
  parent: string | null;
  depth: number;
  /** Nodes of this label itself (not its children); null while unknown. */
  count: number | null;
  properties: string[];
}

/** A relationship type: how many edges, and the label pairs it joins, most edges first. */
export interface RelationshipRow {
  type: string;
  count: number;
  estimated: boolean;
  pairs: { src: string; dst: string; count: number }[];
  /** Derived by a resolve rule (`edges:resolve`), not written directly. */
  derived: boolean;
  /** The label pairs it is declared to join; empty: any pair (open). */
  declared: { src: string; dst: string }[];
  /** Pairs in the data outside the declared ones. */
  undeclared: number;
}

/** Names graphos accepts (the server checks the same patterns). */
export const LABEL_NAME = /^[A-Z][A-Za-z0-9_]{0,62}$/;
export const SEARCH_INDEX_NAME = /^[a-z][a-z0-9_]{0,39}$/;
/** A search index covers at most this many properties. */
export const MAX_INDEX_PROPERTIES = 8;

/** Property names per label, from the catalogue (property -> labels carrying it). */
export function propertiesByLabel(byProperty: Record<string, string[]>): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const [property, labels] of Object.entries(byProperty)) {
    for (const label of labels) out.set(label, [...(out.get(label) ?? []), property]);
  }
  for (const [label, names] of out) out.set(label, names.sort());
  return out;
}

/**
 * The vertex labels as a tree: each parent followed by its children
 * (depth-first, alphabetical), labels without a parent at the top. A
 * parent that is not itself registered is shown as a root.
 */
export function labelTree(
  schema: TenantSchema,
  counts: Map<string, number> | null,
  properties: Map<string, string[]>,
): LabelRow[] {
  const labels = new Set(schema.vertex_labels);
  const children = new Map<string | null, string[]>();
  for (const label of schema.vertex_labels) {
    const parent = schema.label_hierarchy[label];
    const key = parent && labels.has(parent) && parent !== label ? parent : null;
    children.set(key, [...(children.get(key) ?? []), label]);
  }
  const rows: LabelRow[] = [];
  const seen = new Set<string>();
  const visit = (label: string, parent: string | null, depth: number) => {
    if (seen.has(label)) return; // a cycle in the hierarchy: show each label once
    seen.add(label);
    rows.push({
      label,
      parent,
      depth,
      count: counts ? (counts.get(label) ?? 0) : null,
      properties: properties.get(label) ?? [],
    });
    for (const child of (children.get(label) ?? []).sort()) visit(child, label, depth + 1);
  };
  for (const root of (children.get(null) ?? []).sort()) visit(root, null, 0);
  for (const label of [...labels].sort()) visit(label, null, 0); // anything only reachable through a cycle
  return rows;
}

/** The relationship types with their edge counts and label pairs, from the schema and the graph map. */
export function relationshipRows(schema: TenantSchema, map: GraphMap | undefined): RelationshipRow[] {
  const derived = new Set(schema.rule_edge_labels);
  const endpoints = new Endpoints(schema);
  const rows = new Map<string, RelationshipRow>(
    schema.edge_labels.map((type) => [
      type,
      {
        type,
        count: 0,
        estimated: false,
        pairs: [],
        derived: derived.has(type),
        declared: endpoints.declared.get(type) ?? [],
        undeclared: 0,
      },
    ]),
  );
  for (const link of map?.links ?? []) {
    const row = rows.get(link.type);
    if (!row) continue;
    row.count += link.count;
    row.estimated ||= link.estimated;
    row.pairs.push({ src: link.src_label, dst: link.dst_label, count: link.count });
    if (endpoints.check(link.type, link.src_label, link.dst_label).status === "undeclared") row.undeclared += 1;
  }
  for (const row of rows.values()) row.pairs.sort((a, b) => b.count - a.count);
  return [...rows.values()].sort((a, b) => b.count - a.count || a.type.localeCompare(b.type));
}

import { useQuery } from "@tanstack/react-query";
import type { InducedEdgesResponse } from "../api/types";
import { mergeComponentCounts, mergeScans, type ScannedNode } from "../graph/scoreGraph";
import { useTenantSession } from "../session/SessionProvider";
import { useSchema } from "./queries";

/** The most nodes a component is drawn with; a larger one is cut, and says so. */
export const COMPONENT_LIMIT = 300;

export interface MetricGraph {
  nodes: ScannedNode[];
  edges: InducedEdgesResponse["edges"];
  /** More nodes (or relationships among them) exist than are drawn. */
  truncated: boolean;
}

/** The labels to scan and how they nest; a label's scan also returns those under it. */
function useLabels() {
  const schema = useSchema();
  return {
    labels: schema.data?.vertex_labels ?? [],
    hierarchy: schema.data?.label_hierarchy ?? {},
    ready: !!schema.data,
  };
}

/**
 * The tenant's top `count` nodes by a stored metric (`graphos_pagerank`, …),
 * across every label, and the relationships among them. Nodes without the
 * value take no part. Read again after a metrics run in this page (Overview),
 * or after a few minutes.
 */
export function useTopByMetric(property: string, count: number) {
  const { api, tenant } = useTenantSession();
  const { labels, hierarchy, ready } = useLabels();
  return useQuery({
    queryKey: ["topByMetric", tenant, property, count],
    enabled: ready,
    staleTime: 5 * 60_000,
    queryFn: async (): Promise<MetricGraph> => {
      // A scan of several labels sorts within each label in turn: scan each and merge.
      const scans = await Promise.all(
        labels.map(
          async (label) =>
            (
              await api.scanNodes({
                labels: [label],
                project: ["*"],
                limit: count,
                order_by: [{ property, desc: true }],
              })
            ).items,
        ),
      );
      const nodes = mergeScans(scans, hierarchy, property, count);
      if (nodes.length === 0) return { nodes, edges: [], truncated: false };
      const induced = await api.inducedEdges([...new Set(nodes.map((n) => n.id))]);
      return { nodes, edges: induced.edges, truncated: induced.truncated };
    },
  });
}

/** The tenant's connected components and their sizes, largest first (from the last metrics run). */
export function useComponents() {
  const { api, tenant } = useTenantSession();
  const { labels, hierarchy, ready } = useLabels();
  return useQuery({
    queryKey: ["components", tenant],
    enabled: ready,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const counts = await Promise.all(labels.map((label) => api.valueCounts(label, "graphos_component", 100)));
      return mergeComponentCounts(Object.fromEntries(labels.map((label, i) => [label, counts[i]!.groups])), hierarchy);
    },
  });
}

/** The nodes of one component (at most `COMPONENT_LIMIT`) and the relationships among them. */
export function useComponent(id: number | null) {
  const { api, tenant } = useTenantSession();
  const { labels, hierarchy, ready } = useLabels();
  return useQuery({
    queryKey: ["component", tenant, id],
    enabled: ready && id !== null,
    staleTime: 5 * 60_000,
    queryFn: async (): Promise<MetricGraph> => {
      const scans = await Promise.all(
        labels.map(
          async (label) =>
            (
              await api.scanNodes({
                labels: [label],
                project: ["*"],
                limit: COMPONENT_LIMIT + 1,
                where: [{ property: "graphos_component", op: "=", value: id }],
              })
            ).items,
        ),
      );
      const all = mergeScans(scans, hierarchy, "graphos_degree", COMPONENT_LIMIT + 1);
      const nodes = all.slice(0, COMPONENT_LIMIT);
      if (nodes.length === 0) return { nodes, edges: [], truncated: false };
      const induced = await api.inducedEdges([...new Set(nodes.map((n) => n.id))]);
      return { nodes, edges: induced.edges, truncated: all.length > COMPONENT_LIMIT || induced.truncated };
    },
  });
}

import { t } from "../i18n";
import { displayName, type GraphModel } from "./GraphModel";

/** A readable name for a community, in place of its number. */
export interface GroupName {
  /** The group's best-connected node on the canvas, e.g. "InvoiceService". */
  title: string;
  /** What the group is made of, e.g. "mostly Class" or "Function, Class". */
  detail: string;
}

/**
 * Names communities after their members on the canvas: the member with the
 * most connections (the node the group forms around) and the labels its
 * members have. Community ids are arbitrary numbers from the clustering;
 * names like these are what the user can recognise. Ties go to the name
 * that sorts first, so a name is stable while the canvas does not change.
 */
export function nameGroups(model: GraphModel, groups: Iterable<string>): Map<string, GroupName> {
  const wanted = new Set(groups);
  const hubs = new Map<string, { name: string; degree: number }>();
  const labels = new Map<string, Map<string, number>>();
  model.graph.forEachNode((key, node) => {
    const group = node.properties.community_id;
    if ((typeof group !== "number" && typeof group !== "string") || !wanted.has(String(group))) return;
    const id = String(group);
    const name = displayName(node.properties, node.id);
    const degree = model.graph.degree(key);
    const hub = hubs.get(id);
    if (!hub || degree > hub.degree || (degree === hub.degree && name.localeCompare(hub.name) < 0)) {
      hubs.set(id, { name, degree });
    }
    const counts = labels.get(id) ?? new Map<string, number>();
    counts.set(node.vertexLabel, (counts.get(node.vertexLabel) ?? 0) + 1);
    labels.set(id, counts);
  });

  const names = new Map<string, GroupName>();
  for (const [id, hub] of hubs) {
    const counts = [...(labels.get(id) ?? new Map<string, number>()).entries()].sort(
      (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]),
    );
    const total = counts.reduce((sum, [, n]) => sum + n, 0);
    const [top, second] = counts;
    const detail =
      top && top[1] / total >= 0.5
        ? t("views.explore.groups.mostly", { label: top[0] })
        : [top?.[0], second?.[0]].filter(Boolean).join(", ");
    names.set(id, { title: hub.name, detail });
  }
  return names;
}

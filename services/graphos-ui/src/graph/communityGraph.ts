import forceAtlas2 from "graphology-layout-forceatlas2";
import type { CommunityMap } from "../api/types";
import { t } from "../i18n";
import { rankColors } from "../theme/categories";
import type { Categories } from "./canvasStyle";
import { bubbleSize, type MapLinkCount } from "./graphMap";
import { displayName, GraphModel } from "./GraphModel";

export type Community = CommunityMap["communities"][number];

/** The key of a group's bubble. */
export function groupKey(id: number): string {
  return `group:${id}`;
}

/** A group's name: its best-connected member, else its number. */
export function groupTitle(community: Community): string {
  const hub = community.hub;
  return hub
    ? displayName(hub.properties, hub.id)
    : t("views.explore.groups.fallbackName", { id: community.community_id });
}

/** What a group is made of: "mostly Class" when one label is a majority, else its two commonest labels. */
export function groupMakeup(community: Community): string {
  const entries = Object.entries(community.labels);
  const [top, second] = entries;
  if (!top) return "";
  if (top[1] / community.size >= 0.5) return t("views.explore.groups.mostly", { label: top[0] });
  return [top[0], second?.[0]].filter(Boolean).join(", ");
}

/**
 * The groups as a graph: one bubble per group (sized by members, named
 * after its hub), one line per pair of groups with edges between them,
 * thicker with more. Laid out with ForceAtlas2, stronger links pulling
 * harder, bubbles kept apart by their size.
 */
export function buildCommunityModel(map: CommunityMap): GraphModel {
  const model = new GraphModel();
  const graph = model.graph;
  map.communities.forEach((community, i) => {
    const angle = i * 2.399963;
    const r = Math.sqrt(i + 1);
    graph.addNode(groupKey(community.community_id), {
      id: String(community.community_id),
      vertexLabel: groupMakeup(community),
      properties: { name: groupTitle(community), count: community.size },
      size: bubbleSize(community.size),
      x: r * Math.cos(angle),
      y: r * Math.sin(angle),
    });
  });
  for (const link of map.links) {
    const [src, dst] = [groupKey(link.src), groupKey(link.dst)];
    if (!graph.hasNode(src) || !graph.hasNode(dst)) continue;
    graph.addDirectedEdgeWithKey(`${src}|${dst}`, src, dst, {
      edgeType: link.types[0]?.type ?? "",
      properties: { count: link.count, types: link.types as MapLinkCount[] },
    });
  }
  if (graph.order > 2) {
    forceAtlas2.assign(graph, {
      iterations: 500,
      getEdgeWeight: (_edge, attributes) => Math.log10(Number(attributes.properties.count) + 1),
      settings: { ...forceAtlas2.inferSettings(graph), adjustSizes: true, scalingRatio: 60, gravity: 0.4 },
    });
  }
  return model;
}

/** The groups' colours: by size rank, keyed by bubble. */
export function communityCategories(map: CommunityMap): Categories {
  const ranked = map.communities.map((c) => [groupKey(c.community_id), c.size] as [string, number]);
  return { colorBy: "group", ranked, colors: rankColors(ranked.map(([key]) => key)), uncategorised: 0 };
}

/** The groups `id` has edges to and from, most edges first. */
export function groupLinks(
  map: CommunityMap,
  id: number,
): {
  outgoing: { id: number; count: number; types: MapLinkCount[] }[];
  incoming: { id: number; count: number; types: MapLinkCount[] }[];
} {
  const byCount = (a: { count: number }, b: { count: number }) => b.count - a.count;
  return {
    outgoing: map.links
      .filter((l) => l.src === id)
      .map((l) => ({ id: l.dst, count: l.count, types: l.types }))
      .sort(byCount),
    incoming: map.links
      .filter((l) => l.dst === id)
      .map((l) => ({ id: l.src, count: l.count, types: l.types }))
      .sort(byCount),
  };
}

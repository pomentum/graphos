import type { CommunityMap } from "../src/api/types";
import {
  buildCommunityModel,
  communityCategories,
  groupKey,
  groupLinks,
  groupMakeup,
  groupTitle,
  shortName,
} from "../src/graph/communityGraph";
import { CATEGORY_COLORS } from "../src/theme/categories";

const map: CommunityMap = {
  available: true,
  computed_at: "2026-10-07T10:00:00Z",
  modularity: 0.8,
  n_communities: 3,
  communities: [
    {
      community_id: 4,
      size: 100,
      labels: { Function: 70, Class: 30 },
      hub: { id: "fn:pay", label: "Function", properties: { name: "pay" }, degree: 40 },
      internal_edges: 300,
    },
    { community_id: 9, size: 50, labels: { Class: 20, File: 20, Folder: 10 }, hub: null, internal_edges: 80 },
  ],
  links: [
    { src: 4, dst: 9, count: 12, types: [{ type: "CALLS", count: 12 }] },
    { src: 9, dst: 4, count: 3, types: [{ type: "USES", count: 3 }] },
    { src: 4, dst: 77, count: 5, types: [{ type: "CALLS", count: 5 }] },
  ],
};

describe("groups view", () => {
  it("names groups after their hub, or their number", () => {
    expect(groupTitle(map.communities[0]!)).toBe("pay");
    expect(groupTitle(map.communities[1]!)).toBe("Group 9");
    expect(groupMakeup(map.communities[0]!)).toBe("mostly Function");
    expect(groupMakeup(map.communities[1]!)).toBe("Class, File");
  });

  it("draws one bubble per group and lines only between shown groups", () => {
    const model = buildCommunityModel(map);
    expect(model.graph.order).toBe(2);
    expect(model.graph.size).toBe(2); // the link to group 77 (not shown) is left out
    expect(model.graph.getNodeAttribute(groupKey(4), "properties")).toEqual({
      name: "pay",
      fullName: "pay",
      count: 100,
    });
    expect(communityCategories(map).colors.get(groupKey(4))).toBe(CATEGORY_COLORS[0]);
  });

  it("lists a group's links both ways, most edges first", () => {
    expect(groupLinks(map, 4).outgoing.map((l) => [l.id, l.count])).toEqual([
      [9, 12],
      [77, 5],
    ]);
    expect(groupLinks(map, 4).incoming.map((l) => [l.id, l.count])).toEqual([[9, 3]]);
  });
});

describe("short group names", () => {
  it("keeps the meaningful end of id-like names", () => {
    expect(shortName("repo/src/main/java/com/example/Invoice.java::__file__")).toBe("Invoice.java");
    expect(shortName("parent:com.example.billing.invoice.core")).toBe("invoice.core");
    expect(shortName("billing-api")).toBe("billing-api");
    expect(shortName("a/b/" + "x".repeat(60))).toHaveLength(28);
  });
});

describe("thinning the lines", () => {
  it("keeps each bubble's strongest links only", () => {
    const groups: CommunityMap = {
      ...map,
      communities: Array.from({ length: 7 }, (_, i) => ({
        community_id: i,
        size: 10,
        labels: { Doc: 10 },
        hub: null,
        internal_edges: 0,
      })),
      // Group 0 links to every other group, with counts 1..6.
      links: Array.from({ length: 6 }, (_, i) => ({
        src: 0,
        dst: i + 1,
        count: i + 1,
        types: [{ type: "X", count: i + 1 }],
      })),
    };
    const model = buildCommunityModel(groups);
    // Every edge is the only (so strongest) link of its other end: all are kept.
    expect(model.graph.size).toBe(6);
    expect(shortName("party-management:party-mgt")).toBe("party-mgt");
    expect(shortName("http://example.org/a")).toBe("a");
  });
});

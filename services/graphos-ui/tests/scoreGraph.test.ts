import { buildScoreModel, heatColor, mergeComponentCounts, mergeScans, rankHeat } from "../src/graph/scoreGraph";

const node = (id: string, label: string, pagerank?: number) => ({
  id,
  label,
  properties: pagerank === undefined ? { id } : { id, graphos_pagerank: pagerank },
});

describe("mergeScans", () => {
  it("keeps a node once, under its most specific label, ranked across labels", () => {
    // A scan of Package also returns its Library nodes, named Package.
    const hierarchy = { Library: "Package", Tool: "Package" };
    const merged = mergeScans(
      [
        [node("a", "Package", 0.5), node("b", "Package", 0.2)],
        [node("a", "Library", 0.5)],
        [node("b", "Tool", 0.2), node("c", "Tool", 0.3)],
        [node("p", "Person", 0.4)],
      ],
      hierarchy,
      "graphos_pagerank",
      10,
    );
    expect(merged.map((n) => `${n.label}:${n.id}`)).toEqual(["Library:a", "Person:p", "Tool:c", "Tool:b"]);
  });

  it("keeps the same id in unrelated labels, and cuts to the limit", () => {
    const merged = mergeScans([[node("x", "User", 1)], [node("x", "Document", 2)]], {}, "graphos_pagerank", 1);
    expect(merged.map((n) => n.label)).toEqual(["Document"]);
  });
});

describe("components and heat", () => {
  it("sums each component over the labels, counting a parent label's nodes once", () => {
    // Package's histogram also counts its Library and Tool nodes.
    const merged = mergeComponentCounts(
      {
        Package: [
          { value: "0", count: 240 },
          { value: "1", count: 2 },
        ],
        Library: [
          { value: "0", count: 188 },
          { value: "1", count: 1 },
        ],
        Tool: [
          { value: "0", count: 49 },
          { value: "1", count: 1 },
        ],
        Person: [
          { value: "0", count: 90 },
          { value: "", count: 4 },
        ],
      },
      { Library: "Package", Tool: "Package" },
    );
    expect(merged).toEqual([
      { id: 0, size: 188 + 49 + 3 + 90 },
      { id: 1, size: 2 },
    ]);
  });

  it("colours the first rank hottest and the last coolest", () => {
    const heat = rankHeat(["a", "b", "c"]);
    expect(heat.colors.get("a")).toBe(heatColor(1));
    expect(heat.colors.get("c")).toBe(heatColor(0));
    expect(heatColor(0)).not.toBe(heatColor(1));
  });

  it("draws the relationships among the scanned nodes only", () => {
    const model = buildScoreModel(
      [node("a", "Library", 0.5), node("b", "Library", 0.1)],
      [
        { src_id: "b", dst_id: "a", type: "DEPENDS_ON", properties: {} },
        { src_id: "b", dst_id: "zz", type: "DEPENDS_ON", properties: {} },
      ],
      "graphos_pagerank",
    );
    expect(model.graph.order).toBe(2);
    expect(model.graph.size).toBe(1);
    expect(model.graph.getNodeAttribute("Library:a", "size")).toBeGreaterThan(
      model.graph.getNodeAttribute("Library:b", "size"),
    );
  });
});

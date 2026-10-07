import type { GraphMap } from "../src/api/types";
import {
  bubbleSize,
  buildMapModel,
  linksOf,
  mapCategories,
  mapEdgeDisplay,
  mapNodeDisplay,
} from "../src/graph/graphMap";
import { GraphModel } from "../src/graph/GraphModel";
import { CATEGORY_COLORS } from "../src/theme/categories";
import { DARK } from "../src/theme/tokens";

const map: GraphMap = {
  estimated: true,
  labels: [
    { label: "Function", count: 1000 },
    { label: "Class", count: 100 },
    { label: "File", count: 10 },
  ],
  links: [
    { src_label: "Function", type: "CALLS", dst_label: "Function", count: 5000, estimated: true },
    { src_label: "Class", type: "DEFINES", dst_label: "Function", count: 900, estimated: false },
    { src_label: "Class", type: "USES", dst_label: "Function", count: 100, estimated: false },
    { src_label: "File", type: "CONTAINS", dst_label: "Class", count: 100, estimated: false },
  ],
};

describe("graph map", () => {
  it("draws one bubble per label and one line per linked pair, summing its types", () => {
    const model = buildMapModel(map);
    expect(model.graph.order).toBe(3);
    expect(model.graph.size).toBe(2); // Function→Function is listed, not drawn
    const pair = model.graph.getEdgeAttribute("Class|Function", "properties");
    expect(pair).toEqual({
      count: 1000,
      types: [
        { type: "DEFINES", count: 900 },
        { type: "USES", count: 100 },
      ],
    });
    expect(bubbleSize(1000)).toBeGreaterThan(bubbleSize(10));
  });

  it("lists what a label links to and from, most edges first", () => {
    expect(linksOf(map, "Function")).toEqual({
      outgoing: [{ label: "Function", type: "CALLS", count: 5000 }],
      incoming: [
        { label: "Class", type: "DEFINES", count: 900 },
        { label: "Class", type: "USES", count: 100 },
      ],
    });
  });

  it("colours labels by size and labels each with its count", () => {
    const categories = mapCategories(map);
    const model = buildMapModel(map);
    const emphasis = {
      selected: null,
      categories,
      focus: { nodes: new Set<string>(), edges: new Set<string>() },
      recent: new Set<string>(),
    };
    const fn = mapNodeDisplay("Function", model.graph.getNodeAttributes("Function"), emphasis, DARK);
    expect(fn).toMatchObject({ color: CATEGORY_COLORS[0], label: "Function · 1,000", forceLabel: true });
    const selected = { ...emphasis, selected: "Class", focus: model.neighborhood("Class") };
    expect(mapNodeDisplay("Class", model.graph.getNodeAttributes("Class"), selected, DARK).color).toBe(DARK.accent);
    expect(
      mapEdgeDisplay("Class|Function", model.graph.getEdgeAttributes("Class|Function"), selected, DARK).color,
    ).toBe(DARK.accent);
  });
});

describe("GraphModel.addGroup", () => {
  it("adds nodes and their edges as one step, beside what is on the canvas", () => {
    const model = new GraphModel();
    const existing = model.addNode({ id: "x", label: "Doc" }, {});
    model.graph.mergeNodeAttributes(existing, { x: 0, y: 0 });
    const keys = model.addGroup(
      "Open Function",
      ["a", "b", "c"].map((id) => ({ ref: { id, label: "Function" }, properties: { name: id } })),
      [{ type: "CALLS", src: { id: "a", label: "Function" }, dst: { id: "b", label: "Function" } }],
    );
    expect(keys).toHaveLength(3);
    expect(model.graph.size).toBe(1);
    expect(keys.every((key) => model.graph.getNodeAttribute(key, "x") > 0)).toBe(true);
    expect(model.lastStep).toBe("Open Function");
    model.undo();
    expect(model.graph.order).toBe(1);
  });
});

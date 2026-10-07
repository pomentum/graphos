import {
  edgeDisplay,
  FOCUS_LABEL_LIMIT,
  categorize,
  nodeCategory,
  nodeDisplay,
  type Categories,
  type Emphasis,
} from "../src/graph/canvasStyle";
import { GraphModel, type NodeAttributes } from "../src/graph/GraphModel";
import { CATEGORY_COLORS } from "../src/theme/categories";
import { DARK } from "../src/theme/tokens";

const node: NodeAttributes = { id: "a", vertexLabel: "Doc", properties: { name: "Alpha" }, x: 0, y: 0, size: 5 };
const emphasis = (selected: string | null, focus: string[] = [], recent: string[] = []): Emphasis => ({
  selected,
  focus: { nodes: new Set(focus), edges: new Set(["e1"]) },
  recent: new Set(recent),
});

describe("canvas style", () => {
  it("draws the selection in the accent, always labelled", () => {
    expect(nodeDisplay("k", node, emphasis("k"), DARK)).toMatchObject({
      color: DARK.accent,
      label: "Alpha",
      forceLabel: true,
    });
  });

  it("labels highlighted nodes only while there are few", () => {
    const few = nodeDisplay("k", node, emphasis(null, ["k"]), DARK);
    const many = nodeDisplay(
      "k",
      node,
      emphasis(
        null,
        Array.from({ length: FOCUS_LABEL_LIMIT + 1 }, (_, i) => (i ? `n${i}` : "k")),
      ),
      DARK,
    );
    expect(few.forceLabel).toBe(true);
    expect(many.forceLabel).toBe(false);
  });

  it("leaves everything else grey and unlabelled", () => {
    expect(nodeDisplay("k", node, emphasis(null), DARK)).toMatchObject({ color: DARK.nodeIdle, forceLabel: false });
    expect(nodeDisplay("k", node, emphasis(null, [], ["k"]), DARK).color).toBe(DARK.accentText);
    expect(edgeDisplay("e1", { edgeType: "X", properties: {} }, emphasis(null), DARK).color).toBe(DARK.accent);
    expect(edgeDisplay("e2", { edgeType: "X", properties: {} }, emphasis(null), DARK).color).toBe(DARK.edgeIdle);
  });
});

describe("colour by category", () => {
  const grouped: NodeAttributes = { ...node, properties: { name: "Alpha", community_id: 7 } };
  const canvas = (labels: string[], group?: number) => {
    const model = new GraphModel();
    labels.forEach((label, i) =>
      model.addNode({ id: `n${i}`, label }, group === undefined ? {} : { community_id: group }),
    );
    return model;
  };

  it("ranks categories by size and gives each of the first ten its own colour", () => {
    const labels = ["B", "A", "A", "C", "C", "C", ...Array.from({ length: 10 }, (_, i) => `Z${i}`)];
    const categories = categorize(canvas(labels), "label");
    expect(categories.ranked.slice(0, 3)).toEqual([
      ["C", 3],
      ["A", 2],
      ["B", 1],
    ]);
    expect(categories.colors.get("C")).toBe(CATEGORY_COLORS[0]);
    expect(categories.colors.size).toBe(CATEGORY_COLORS.length);
    expect(new Set(categories.colors.values()).size).toBe(CATEGORY_COLORS.length);
    expect([...categories.colors.values()]).not.toContain(DARK.accent);
  });

  it("colours idle nodes by their category, and the rest grey", () => {
    const byLabel = categorize(canvas(["Doc"]), "label");
    const byGroup = categorize(canvas(["Doc", "Doc"], 7), "group");
    const draw = (n: NodeAttributes, categories?: Categories) =>
      nodeDisplay("k", n, { ...emphasis(null), categories }, DARK).color;
    expect(draw(grouped)).toBe(DARK.nodeIdle);
    expect(draw(grouped, byLabel)).toBe(CATEGORY_COLORS[0]);
    expect(draw(grouped, byGroup)).toBe(CATEGORY_COLORS[0]);
    expect(draw({ ...node, vertexLabel: "Other" }, byLabel)).toBe(DARK.nodeIdle);
    expect(draw(node, byGroup)).toBe(DARK.nodeIdle);
    expect(nodeDisplay("k", grouped, { ...emphasis("k"), categories: byLabel }, DARK).color).toBe(DARK.accent);
  });

  it("counts nodes outside any group", () => {
    const model = canvas(["Doc"], 1);
    model.addNode({ id: "loose", label: "Doc" }, {});
    expect(categorize(model, "group")).toMatchObject({ ranked: [["1", 1]], uncategorised: 1 });
    expect(categorize(model, "none")).toMatchObject({ ranked: [], uncategorised: 0 });
  });

  it("names the category a node falls in", () => {
    expect(nodeCategory(grouped, "label")).toBe("Doc");
    expect(nodeCategory(grouped, "group")).toBe("7");
    expect(nodeCategory({ ...node, properties: { community_id: { x: 1 } } }, "group")).toBeNull();
    expect(nodeCategory(grouped, "none")).toBeNull();
  });

  it("draws the selected edge thicker than the focus", () => {
    const withEdge = { ...emphasis(null), selectedEdge: "e2" };
    const edge = { edgeType: "X", properties: {} };
    expect(edgeDisplay("e2", edge, withEdge, DARK)).toEqual({ color: DARK.accent, size: 4, zIndex: 2 });
    expect(edgeDisplay("e1", edge, withEdge, DARK).size).toBe(2);
  });
});

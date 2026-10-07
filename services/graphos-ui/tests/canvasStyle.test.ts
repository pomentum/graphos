import { edgeDisplay, FOCUS_LABEL_LIMIT, nodeDisplay, type Emphasis } from "../src/graph/canvasStyle";
import type { NodeAttributes } from "../src/graph/GraphModel";
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

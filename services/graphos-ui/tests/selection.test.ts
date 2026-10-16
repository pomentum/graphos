import { nodeDisplay, type Emphasis } from "../src/graph/canvasStyle";
import { GraphModel, nodeKey, type NodeAttributes } from "../src/graph/GraphModel";
import { insidePolygon, selectedNodes, selectionCsv, toggled } from "../src/graph/selection";
import { canvasJson } from "../src/lib/canvasExport";
import { labelStyles } from "../src/theme/labelStyles";
import { DARK } from "../src/theme/tokens";

vi.mock("@sigma/export-image", () => ({ toBlob: vi.fn() }));

const square = [
  { x: 0, y: 0 },
  { x: 10, y: 0 },
  { x: 10, y: 10 },
  { x: 0, y: 10 },
];

function canvas(): { model: GraphModel; a: string; b: string; c: string } {
  const model = new GraphModel();
  const a = model.addNode({ id: "a", label: "Doc" }, { name: "Alpha", pages: 3 });
  const b = model.addNode({ id: "b", label: "Doc" }, { name: "Beta, the second" }, a);
  const c = model.addNode({ id: "c", label: "Person" }, { name: "Carol" }, a);
  model.addEdge("CITES", a, b);
  model.addEdge("WROTE", c, a);
  return { model, a, b, c };
}

describe("selection", () => {
  it("finds points inside a drawn loop, not outside it", () => {
    expect(insidePolygon({ x: 5, y: 5 }, square)).toBe(true);
    expect(insidePolygon({ x: 15, y: 5 }, square)).toBe(false);
    const hollow = [
      { x: 0, y: 0 },
      { x: 10, y: 0 },
      { x: 10, y: 10 },
      { x: 5, y: 2 },
      { x: 0, y: 10 },
    ];
    expect(insidePolygon({ x: 5, y: 8 }, hollow)).toBe(false);
  });

  it("toggles keys in and out of a selection", () => {
    expect(toggled(new Set(["a", "b"]), ["b", "c"])).toEqual(new Set(["a", "c"]));
  });

  it("leaves out nodes that are gone and placeholders", () => {
    const { model, a, b } = canvas();
    model.forget(b);
    expect(selectedNodes(model, new Set([a, b, "more:x"]))).toEqual([a]);
  });

  it("exports the selected nodes and only the edges between them", () => {
    const { model, a, b } = canvas();
    const json = JSON.parse(canvasJson(model.graph, new Set([a, b])));
    expect(json.nodes.map((n: { id: string }) => n.id)).toEqual(["a", "b"]);
    expect(json.edges).toEqual([
      { src_id: "a", src_label: "Doc", dst_id: "b", dst_label: "Doc", type: "CITES", properties: {} },
    ]);
  });

  it("writes the selection as CSV with every property any node carries", () => {
    const { model, a, b } = canvas();
    const lines = selectionCsv(model, new Set([a, b]))
      .replace(/^﻿/, "")
      .split("\r\n");
    expect(lines[0]).toBe("label,id,name,pages");
    expect(lines[1]).toBe("Doc,a,Alpha,3");
    expect(lines[2]).toBe('Doc,b,"Beta, the second",');
  });
});

describe("canvas model, several nodes at once", () => {
  it("hides several nodes as one step that Back restores", () => {
    const { model, a, b, c } = canvas();
    model.removeNodes([a, b]);
    expect(model.graph.order).toBe(1);
    expect(model.lastStep).toBe("Hide 2 nodes");
    model.undo();
    expect(model.graph.hasNode(a) && model.graph.hasNode(b) && model.graph.hasEdge(model.graph.edges(c)[0]!)).toBe(
      true,
    );
  });

  it("places a node where it was asked for", () => {
    const model = new GraphModel();
    const key = model.addNodeAt({ id: "n", label: "Doc" }, {}, { x: 12, y: -4 });
    expect(key).toBe(nodeKey({ id: "n", label: "Doc" }));
    expect(model.graph.getNodeAttributes(key)).toMatchObject({ x: 12, y: -4 });
  });
});

describe("canvas style, selection and zoom", () => {
  const node: NodeAttributes = { id: "a", vertexLabel: "Doc", properties: { name: "Alpha" }, x: 0, y: 0, size: 5 };
  const base: Emphasis = { selected: null, focus: { nodes: new Set(), edges: new Set() }, recent: new Set() };

  it("draws nodes picked together like the selection", () => {
    expect(nodeDisplay("k", node, { ...base, multi: new Set(["k", "j"]) }, DARK)).toMatchObject({
      color: DARK.accent,
      forceLabel: true,
    });
  });

  it("leaves out pictograms and hub labels far out", () => {
    const styles = labelStyles({ label_hierarchy: {}, display: { vertex: { Doc: { icon: "document" } } } } as never);
    const near = nodeDisplay("k", node, { ...base, styles, hubs: new Set(["k"]) }, DARK);
    const far = nodeDisplay("k", node, { ...base, styles, hubs: new Set(["k"]), far: true }, DARK);
    expect(near).toMatchObject({ type: "icon", forceLabel: true });
    expect(far).toMatchObject({ type: "circle", forceLabel: false });
  });
});

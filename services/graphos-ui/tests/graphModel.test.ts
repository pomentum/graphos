import type { Neighbor } from "../src/api/types";
import { edgeKey, GraphModel, moreKey, nodeKey } from "../src/graph/GraphModel";

const neighbor = (id: string, direction: "in" | "out", type = "CALLS"): Neighbor => ({
  neighbor_id: id,
  neighbor_label: "Function",
  neighbor_properties: { name: id },
  direction,
  edge_type: type,
  edge_properties: {},
});

describe("GraphModel", () => {
  it("adds neighbours with edges pointing the right way", () => {
    const model = new GraphModel();
    const center = model.addNode({ id: "a", label: "Function" }, { name: "a" });
    model.addNeighbors(center, [neighbor("b", "out"), neighbor("c", "in")]);
    const b = nodeKey({ id: "b", label: "Function" });
    const c = nodeKey({ id: "c", label: "Function" });
    expect(model.graph.hasEdge(edgeKey("CALLS", center, b))).toBe(true);
    expect(model.graph.hasEdge(edgeKey("CALLS", c, center))).toBe(true);
    expect(model.neighborhood(center).nodes).toEqual(new Set([center, b, c]));
  });

  it("keeps one node per label and id, merging new properties", () => {
    const model = new GraphModel();
    model.addNode({ id: "a", label: "Doc" }, { name: "A" });
    model.addNode({ id: "a", label: "Doc" }, { size: 2 });
    model.addNode({ id: "a", label: "Person" }, {});
    expect(model.graph.order).toBe(2);
    expect(model.graph.getNodeAttribute(nodeKey({ id: "a", label: "Doc" }), "properties")).toEqual({
      name: "A",
      size: 2,
    });
  });

  it("returns the nodes and edges of paths for highlighting", () => {
    const model = new GraphModel();
    const focus = model.addPaths([
      {
        hops: 1,
        nodes: [
          { id: "a", label: "Doc", properties: null },
          { id: "b", label: "Doc", properties: null },
        ],
        edges: [{ type: "CITES", src_id: "b", dst_id: "a" }],
      },
    ]);
    const a = nodeKey({ id: "a", label: "Doc" });
    const b = nodeKey({ id: "b", label: "Doc" });
    expect(focus.nodes).toEqual(new Set([a, b]));
    expect(focus.edges).toEqual(new Set([edgeKey("CITES", b, a)]));
  });

  it("notifies listeners and counts revisions", () => {
    const model = new GraphModel();
    const seen = vi.fn();
    const stop = model.subscribe(seen);
    const key = model.addNode({ id: "a", label: "Doc" }, {});
    model.removeNode(key);
    stop();
    model.clear();
    expect(seen).toHaveBeenCalledTimes(2);
    expect(model.revision).toBe(3);
    expect(model.isEmpty).toBe(true);
  });
});

describe("GraphModel.load", () => {
  it("replaces the canvas with an exported graph, mapping edges onto label-scoped keys", () => {
    const model = new GraphModel();
    model.addNode({ id: "old", label: "Doc" }, {});
    const drawn = model.load({
      nodes: [
        { key: "a", attributes: { _kind: "Person", label: "Ada", name: "Ada" } },
        { key: "b", attributes: { _kind: "Doc", label: "b" } },
        { key: "c", attributes: { _kind: "Doc", label: "c" } },
      ],
      edges: [
        { key: "e1", source: "a", target: "b", attributes: { _type: "WROTE", label: "WROTE" } },
        { key: "e2", source: "a", target: "missing", attributes: { _type: "WROTE", label: "WROTE" } },
      ],
    });
    expect(drawn).toEqual({ nodes: 3, edges: 1 });
    const a = nodeKey({ id: "a", label: "Person" });
    expect(model.graph.hasNode(nodeKey({ id: "old", label: "Doc" }))).toBe(false);
    expect(model.graph.getNodeAttributes(a)).toMatchObject({
      id: "a",
      vertexLabel: "Person",
      properties: { name: "Ada" },
    });
    expect(model.graph.hasEdge(edgeKey("WROTE", a, nodeKey({ id: "b", label: "Doc" })))).toBe(true);
    const positions = model.graph.mapNodes((_, attrs) => `${attrs.x},${attrs.y}`);
    expect(new Set(positions).size).toBe(3);
  });
});

describe("GraphModel edges", () => {
  it("addresses an edge by its type and both ends, and replaces its properties", () => {
    const model = new GraphModel();
    const a = model.addNode({ id: "a", label: "Function" }, { name: "a" });
    model.addNeighbors(a, [neighbor("b", "in")]);
    const key = edgeKey("CALLS", nodeKey({ id: "b", label: "Function" }), a);
    expect(model.edgeRef(key)).toEqual({
      type: "CALLS",
      srcId: "b",
      srcLabel: "Function",
      dstId: "a",
      dstLabel: "Function",
    });
    const before = model.revision;
    model.updateEdge(key, { weight: 2 });
    expect(model.graph.getEdgeAttribute(key, "properties")).toEqual({ weight: 2 });
    expect(model.revision).toBe(before + 1);
    model.updateEdge("gone", { weight: 3 });
    expect(model.revision).toBe(before + 1);
  });
});

describe("GraphModel history", () => {
  const many = (n: number) => Array.from({ length: n }, (_, i) => neighbor(`n${i}`, "out"));

  it("undoes expands, hides and collapses step by step", () => {
    const model = new GraphModel();
    const center = model.addNode({ id: "a", label: "Function" }, { name: "a" });
    const added = model.addNeighbors(center, many(3));
    expect(added).toHaveLength(3);
    expect(model.lastStep).toBe("Expand a");

    model.removeNode(added[0]!);
    expect(model.graph.hasNode(added[0]!)).toBe(false);
    model.undo();
    expect(model.graph.hasNode(added[0]!)).toBe(true);
    expect(model.graph.hasEdge(edgeKey("CALLS", center, added[0]!))).toBe(true);

    model.undo(); // the expand
    expect(model.graph.order).toBe(1);
    model.undo(); // the first node
    expect(model.isEmpty).toBe(true);
    expect(model.lastStep).toBeNull();
  });

  it("returns only nodes that were not on the canvas yet", () => {
    const model = new GraphModel();
    const center = model.addNode({ id: "a", label: "Function" }, {});
    model.addNode({ id: "n0", label: "Function" }, {});
    expect(model.addNeighbors(center, many(2))).toEqual([nodeKey({ id: "n1", label: "Function" })]);
  });

  it("collapses what an expand added, keeping nodes linked to anything else", () => {
    const model = new GraphModel();
    const center = model.addNode({ id: "a", label: "Function" }, {});
    const [kept, gone] = model.addNeighbors(center, many(2));
    const other = model.addNode({ id: "z", label: "Function" }, {});
    model.addEdge("CALLS", kept!, other);
    expect(model.canCollapse(center)).toBe(true);
    model.collapse(center);
    expect(model.graph.hasNode(kept!)).toBe(true);
    expect(model.graph.hasNode(gone!)).toBe(false);
    expect(model.canCollapse(center)).toBe(false);
    model.undo();
    expect(model.graph.hasNode(gone!)).toBe(true);
  });

  it("keeps a +N more placeholder for neighbours not added yet", () => {
    const model = new GraphModel();
    const center = model.addNode({ id: "a", label: "Function" }, {});
    model.addNeighbors(center, many(2), 30);
    expect(model.graph.getNodeAttributes(moreKey(center))).toMatchObject({
      more: 30,
      properties: { name: "+30 more" },
    });
    model.addNeighbors(center, [neighbor("x", "out")], 29);
    expect(model.graph.getNodeAttribute(moreKey(center), "more")).toBe(29);
    model.undo();
    expect(model.graph.getNodeAttribute(moreKey(center), "more")).toBe(30);
    model.addNeighbors(center, [neighbor("x", "out")], 29);
    model.addNeighbors(center, [neighbor("y", "out")], 0);
    expect(model.graph.hasNode(moreKey(center))).toBe(false);
    expect(model.hubs(5)).toEqual([center]);
  });

  it("forgets deleted nodes without making Back restore them", () => {
    const model = new GraphModel();
    const key = model.addNode({ id: "a", label: "Function" }, {});
    model.forget(key);
    expect(model.isEmpty).toBe(true);
    model.undo(); // undoes the add, which is already gone
    expect(model.isEmpty).toBe(true);
  });

  it("fans new neighbours out away from the existing ones, without overlaps", () => {
    const model = new GraphModel();
    const center = model.addNode({ id: "a", label: "Function" }, {});
    model.graph.mergeNodeAttributes(center, { x: 0, y: 0 });
    const left = model.addNode({ id: "l", label: "Function" }, {});
    model.graph.mergeNodeAttributes(left, { x: -2, y: 0 });
    model.addEdge("CALLS", center, left);
    const added = model.addNeighbors(center, many(10));
    const xs = added.map((key) => model.graph.getNodeAttribute(key, "x"));
    expect(xs.filter((x) => x > 0).length).toBeGreaterThan(7);
    const spots = new Set(
      added.map(
        (key) =>
          `${model.graph.getNodeAttribute(key, "x").toFixed(3)},${model.graph.getNodeAttribute(key, "y").toFixed(3)}`,
      ),
    );
    expect(spots.size).toBe(10);
  });

  it("places a second page of neighbours outside the first, not on it", () => {
    const model = new GraphModel();
    const center = model.addNode({ id: "a", label: "Function" }, {});
    const at = (key: string) => {
      const { x, y } = model.graph.getNodeAttributes(key);
      return `${x.toFixed(3)},${y.toFixed(3)}`;
    };
    const first = model.addNeighbors(center, many(20), 20);
    const second = model.addNeighbors(
      center,
      Array.from({ length: 20 }, (_, i) => neighbor(`m${i}`, "out")),
    );
    const spots = new Set([...first, ...second].map(at));
    expect(spots.size).toBe(40);
  });
});

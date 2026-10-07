import type { Neighbor } from "../src/api/types";
import { edgeKey, GraphModel, nodeKey } from "../src/graph/GraphModel";

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

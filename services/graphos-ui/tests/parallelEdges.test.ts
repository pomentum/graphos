import { GraphModel } from "../src/graph/GraphModel";
import { parallelCurvature } from "../src/graph/parallelEdges";

const customer = { id: "c1", label: "Customer" };
const order = { id: "o1", label: "Order" };

describe("edges sharing their two nodes", () => {
  it("are all kept on the canvas: one per type, either way", () => {
    const model = new GraphModel();
    model.addGroup(
      "t",
      [
        { ref: customer, properties: {} },
        { ref: order, properties: {} },
      ],
      [
        { type: "PLACED", src: customer, dst: order },
        { type: "PAID_FOR", src: customer, dst: order },
        { type: "SHIPPED_TO", src: order, dst: customer },
      ],
    );
    expect(model.graph.size).toBe(3);
    // Added again (another expand): nothing doubles.
    model.addGroup("again", [], [{ type: "PLACED", src: customer, dst: order }]);
    expect(model.graph.size).toBe(3);
  });

  it("bend apart, symmetrically, and a lone edge stays straight", () => {
    const model = new GraphModel();
    const other = { id: "o2", label: "Order" };
    model.addGroup(
      "t",
      [customer, order, other].map((ref) => ({ ref, properties: {} })),
      [
        { type: "A", src: customer, dst: order },
        { type: "B", src: customer, dst: order },
        { type: "C", src: order, dst: customer },
        { type: "A", src: customer, dst: other },
      ],
    );
    const graph = model.graph;
    const curves = new Map(
      graph
        .edges()
        .map((key) => [graph.getEdgeAttribute(key, "edgeType") + graph.target(key), parallelCurvature(graph, key)]),
    );
    expect(curves.get("AOrder:o2")).toBeNull();
    // Three between c1 and o1: one straight, the others bent to opposite sides (in a common frame).
    const pair = graph.edges().filter((key) => !graph.target(key).endsWith("o2"));
    const frame = pair.map((key) => {
      const c = parallelCurvature(graph, key) ?? 0;
      return graph.source(key) === "Customer:c1" ? c : -c;
    });
    expect(frame.sort((a, b) => a - b)).toEqual([-0.35, 0, 0.35]);
  });
});

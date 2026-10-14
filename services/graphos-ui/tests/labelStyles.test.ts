import ELK from "elkjs/lib/elk.bundled.js";
import { circular, layered, LAYERED_MAX_NODES } from "../src/graph/canvasLayouts";
import { categorize, edgeDisplay, nodeDisplay, styleMaxima, type Emphasis } from "../src/graph/canvasStyle";
import { GraphModel, type NodeAttributes } from "../src/graph/GraphModel";
import { iconImage } from "../src/lib/labelIcons";
import { CATEGORY_COLORS } from "../src/theme/categories";
import { captionOf, labelStyles, styledColors } from "../src/theme/labelStyles";
import { DARK } from "../src/theme/tokens";

const SCHEMA = {
  label_hierarchy: { Employee: "Person", Contractor: "Person" },
  display: {
    vertex: {
      Person: { color: "#5FA584", icon: "person" as const, caption: null, size_by: null },
      Contractor: { color: "#C96F8E", caption: "handle" },
      Doc: { caption: "title", size_by: "pages" },
      Gone: null,
    },
    edge: { WROTE: { color: "#8D97AE", caption: "role" } },
  },
};

const quiet: Emphasis = { selected: null, focus: { nodes: new Set(), edges: new Set() }, recent: new Set() };

describe("label styles", () => {
  it("keeps what each label declares, a child taking its parent's colour and icon", () => {
    const styles = labelStyles(SCHEMA);
    expect(styles.vertex.get("Person")).toEqual({ color: "#5FA584", icon: "person" });
    expect(styles.vertex.get("Employee")).toEqual({ color: "#5FA584", icon: "person" });
    expect(styles.vertex.get("Contractor")).toEqual({ color: "#C96F8E", icon: "person", caption: "handle" });
    expect(styles.vertex.get("Doc")).toEqual({ caption: "title", size_by: "pages" });
    expect(styles.vertex.has("Gone")).toBe(false);
    expect(styles.edge.get("WROTE")).toEqual({ color: "#8D97AE", caption: "role" });
  });

  it("gives declared colours first and the rest the category colours not taken", () => {
    const declared = new Map([["B", { color: CATEGORY_COLORS[0] }]]);
    const colors = styledColors(["A", "B", "C"], declared);
    expect(colors.get("B")).toBe(CATEGORY_COLORS[0]);
    expect(colors.get("A")).toBe(CATEGORY_COLORS[1]);
    expect(colors.get("C")).toBe(CATEGORY_COLORS[2]);
  });

  it("names a node by its label's caption, else name, else the id", () => {
    expect(captionOf({ title: "Guide", name: "g" }, "d1", { caption: "title" })).toBe("Guide");
    expect(captionOf({ name: "g" }, "d1", { caption: "title" })).toBe("g");
    expect(captionOf({ pages: 0 }, "d1", { caption: "pages" })).toBe("0");
    expect(captionOf({}, "d1", undefined)).toBe("d1");
  });

  it("draws the canvas with them: colours, captions, sizes, icons and edge colours", () => {
    const styles = labelStyles(SCHEMA);
    const model = new GraphModel();
    model.addGroup(
      "t",
      [
        { ref: { id: "p1", label: "Employee" }, properties: { name: "Ada" } },
        { ref: { id: "d1", label: "Doc" }, properties: { title: "Guide", pages: 40 } },
        { ref: { id: "d2", label: "Doc" }, properties: { title: "Note", pages: 10 } },
      ],
      [
        {
          type: "WROTE",
          src: { id: "p1", label: "Employee" },
          dst: { id: "d1", label: "Doc" },
          properties: { role: "lead" },
        },
      ],
    );
    const categories = categorize(model, "label", styles);
    expect(categories.colors.get("Employee")).toBe("#5FA584");
    expect(categories.colors.get("Doc")).toBe(CATEGORY_COLORS[0]);

    const emphasis: Emphasis = { ...quiet, categories, styles, styleMax: styleMaxima(model, styles) };
    const employee = model.graph.getNodeAttributes("Employee:p1") as NodeAttributes;
    expect(nodeDisplay("Employee:p1", employee, emphasis, DARK)).toMatchObject({
      color: "#5FA584",
      type: "icon",
      image: iconImage("person"),
      pictoColor: "#FFFFFF",
      label: "Ada",
    });
    const big = nodeDisplay("Doc:d1", model.graph.getNodeAttributes("Doc:d1"), emphasis, DARK);
    const small = nodeDisplay("Doc:d2", model.graph.getNodeAttributes("Doc:d2"), emphasis, DARK);
    expect(big).toMatchObject({ label: "Guide", type: "circle" });
    expect(big.size).toBeGreaterThan(small.size);

    const edge = model.graph.edges()[0]!;
    const attributes = model.graph.getEdgeAttributes(edge);
    const idle = edgeDisplay(edge, attributes, emphasis, DARK);
    expect(idle.color).toBe("#8D97AE");
    expect(idle.label).toBeUndefined(); // captions only on the focus
    const focused = { ...emphasis, focus: { nodes: new Set(["Employee:p1"]), edges: new Set([edge]) } };
    expect(edgeDisplay(edge, attributes, focused, DARK)).toMatchObject({ label: "lead" });
  });
});

describe("canvas layouts", () => {
  function chain(n: number): GraphModel {
    const model = new GraphModel();
    const nodes = Array.from({ length: n }, (_, i) => ({
      ref: { id: `n${i}`, label: i % 2 ? "A" : "B" },
      properties: {},
    }));
    const edges = nodes.slice(1).map((node, i) => ({ type: "NEXT", src: nodes[i]!.ref, dst: node.ref }));
    model.addGroup("t", nodes, edges);
    return model;
  }

  it("puts every node on a circle, label by label", () => {
    const model = chain(8);
    circular(model);
    const radii = model.graph.mapNodes((_, n) => Math.hypot(n.x, n.y));
    expect(Math.max(...radii) - Math.min(...radii)).toBeLessThan(1e-6);
    // The B nodes (sorted first) fill one half of the circle, without an A among them.
    const order = model.graph
      .mapNodes((key, n) => [key, Math.atan2(n.y, n.x)] as const)
      .sort((a, b) => a[1] - b[1])
      .map(([key]) => key.split(":")[0]);
    expect(order.join("").replace(/(.)\1+/g, "$1").length).toBeLessThanOrEqual(3);
  });

  it("lays a chain out in layers, left to right, and refuses a canvas too large", async () => {
    const model = chain(5);
    expect(await layered(model, new ELK())).toBe(true);
    const xs = [0, 1, 2, 3, 4].map((i) => model.graph.getNodeAttribute(`${i % 2 ? "A" : "B"}:n${i}`, "x"));
    expect(xs).toEqual([...xs].sort((a, b) => a - b));
    expect(new Set(xs).size).toBe(5);
    expect(await layered(chain(LAYERED_MAX_NODES + 1), new ELK())).toBe(false);
  });
});

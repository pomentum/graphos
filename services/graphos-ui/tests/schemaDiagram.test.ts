import ELK from "elkjs/lib/elk.bundled.js";
import type { GraphMap, TenantSchema } from "../src/api/types";
import {
  buildDiagram,
  familyColours,
  focusOf,
  freeGeometry,
  layoutKey,
  linkCaption,
  loadPositions,
  placeCaptions,
  captionRect,
  savePositions,
  type Block,
} from "../src/views/schema/diagram/diagramModel";
import { layoutDiagram } from "../src/views/schema/diagram/elkLayout";

const schema: TenantSchema = {
  name: "t",
  vertex_labels: ["Symbol", "Function", "Class", "Test", "File", "Repo", "Draft"],
  edge_labels: ["CALLS", "DEFINES", "CONTAINS", "UNUSED"],
  label_hierarchy: { Function: "Symbol", Class: "Symbol", Test: "Function" },
  search_indexes: {},
  rule_edge_labels: [],
};

const map: GraphMap = {
  labels: [
    { label: "Symbol", count: 5 },
    { label: "Function", count: 120 },
    { label: "Class", count: 30 },
    { label: "Test", count: 8 },
    { label: "File", count: 40 },
    { label: "Repo", count: 2 },
    { label: "Draft", count: 0 },
  ],
  links: [
    { src_label: "Function", type: "CALLS", dst_label: "Function", count: 900, estimated: false },
    { src_label: "File", type: "DEFINES", dst_label: "Function", count: 120, estimated: false },
    { src_label: "File", type: "CONTAINS", dst_label: "Function", count: 60, estimated: true },
    { src_label: "Repo", type: "CONTAINS", dst_label: "File", count: 40, estimated: false },
  ],
  estimated: true,
};

const shape = (item: string | Block): unknown => (typeof item === "string" ? item : item.items.map(shape));

describe("schema diagram", () => {
  it("puts a label and what inherits from it in one block, nested as inheritance nests", () => {
    const diagram = buildDiagram(schema, map);
    expect(diagram.blocks.map(shape)).toEqual([["Symbol", ["Function", "Test"], "Class"]]);
    expect(diagram.blocks[0]!.size).toBe(4);
    // Lone labels busiest first, empty ones last.
    expect(diagram.lone).toEqual(["File", "Repo", "Draft"]);
    expect(diagram.boxes.find((b) => b.label === "Test")!.family).toBe("Symbol");
    expect(diagram.unusedTypes).toEqual(["UNUSED"]);
  });

  it("joins the types of a label pair on one line, busiest first", () => {
    const diagram = buildDiagram(schema, map);
    const line = diagram.links.find((l) => l.key === "rel|File|Function")!;
    expect(line.types.map((t) => t.type)).toEqual(["DEFINES", "CONTAINS"]);
    expect(line.count).toBe(180);
    expect(line.caption).toBe("DEFINES 120 · CONTAINS ≈60");
    expect(diagram.links.some((l) => l.key === "rel|Function|Function")).toBe(true);
    expect(linkCaption(line.types.concat(line.types), String, (n) => `+${n}`)).toBe("DEFINES 120 · CONTAINS ≈60 +2");
  });

  it("leaves out empty labels when asked, and their children stand alone", () => {
    const empty = { ...map, labels: map.labels.map((l) => (l.label === "Symbol" ? { ...l, count: 0 } : l)) };
    const diagram = buildDiagram(schema, empty, { hideEmpty: true });
    expect(diagram.boxes.map((b) => b.label)).not.toContain("Symbol");
    expect(diagram.boxes.map((b) => b.label)).not.toContain("Draft");
    expect(diagram.blocks.map(shape)).toEqual([["Function", "Test"]]);
    expect(diagram.lone).toEqual(["File", "Class", "Repo"]);
  });

  it("survives a cycle in the hierarchy", () => {
    const looped = { ...schema, vertex_labels: ["A", "B"], label_hierarchy: { A: "B", B: "A" } };
    const diagram = buildDiagram(looped, undefined);
    const labels = JSON.stringify([...diagram.blocks.map(shape), ...diagram.lone]);
    expect(labels.match(/"A"/g)).toHaveLength(1);
    expect(labels.match(/"B"/g)).toHaveLength(1);
  });

  it("lays out again only when sizes or blocks change", () => {
    const a = buildDiagram(schema, map);
    const relinked = { ...map, links: map.links.slice(0, 1) };
    expect(layoutKey(buildDiagram(schema, relinked))).toBe(layoutKey(a));
    expect(layoutKey(buildDiagram({ ...schema, label_hierarchy: {} }, map))).not.toBe(layoutKey(a));
  });

  it("brightens a label, its lines and the labels at their other ends", () => {
    const diagram = buildDiagram(schema, map);
    const focus = focusOf(diagram, { kind: "label", id: "File" })!;
    expect([...focus.labels].sort()).toEqual(["File", "Function", "Repo"]);
    expect(focus.links.has("rel|Repo|File")).toBe(true);
    expect(focusOf(diagram, null)).toBeNull();
    expect(familyColours(diagram.blocks, ["#111"]).get("Symbol")).toBe("#111");
  });

  it("packs every label inside its block, without overlaps", async () => {
    const diagram = buildDiagram(schema, map);
    const layout = await layoutDiagram(diagram, new ELK());
    for (const box of diagram.boxes) expect(layout.positions[box.label]).toBeDefined();
    const block = layout.blocks["family:Symbol"]!;
    for (const label of ["Symbol", "Function", "Class", "Test"]) {
      const at = layout.positions[label]!;
      expect(at.x).toBeGreaterThanOrEqual(block.x);
      expect(at.y).toBeGreaterThanOrEqual(block.y);
      expect(at.x).toBeLessThan(block.x + block.w);
      expect(at.y).toBeLessThan(block.y + block.h);
    }
    const nested = layout.blocks["family:Function"]!;
    expect(nested.x).toBeGreaterThanOrEqual(block.x);
    // No two lone labels on top of each other.
    const rects = diagram.lone.map((l) => ({ ...layout.positions[l]!, ...diagram.boxes.find((b) => b.label === l)! }));
    for (const [i, a] of rects.entries()) {
      for (const b of rects.slice(i + 1)) {
        const apart = a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y;
        expect(apart).toBe(true);
      }
    }
  });

  it("draws a line from border to border, and a loop for a label's line to itself", () => {
    const a = { x: 0, y: 0, w: 100, h: 40 };
    const b = { x: 300, y: 0, w: 100, h: 40 };
    expect(freeGeometry(a, b, false, false).d).toMatch(/^M 50 /);
    expect(freeGeometry(a, a, true, false).d).toContain(" C ");
  });

  it("places captions clear of each other and of the boxes", () => {
    // Two parallel lines whose middles nearly meet, and a box in the way.
    const curveA = { a: { x: 0, y: 0 }, c: { x: 200, y: 0 }, b: { x: 400, y: 0 } };
    const curveB = { a: { x: 0, y: 10 }, c: { x: 200, y: 10 }, b: { x: 400, y: 10 } };
    const box = { x: 120, y: 5, w: 60, h: 40 };
    const placed = placeCaptions(
      [
        { key: "a", text: "CALLS 12", curve: curveA, at: { x: 200, y: 0 }, priority: 2 },
        { key: "b", text: "USES 3", curve: curveB, at: { x: 200, y: 10 }, priority: 1 },
      ],
      [box],
    );
    expect(placed.a).toEqual({ x: 200, y: 0 }); // first choice: the middle
    const a = captionRect("CALLS 12", placed.a!);
    const b = captionRect("USES 3", placed.b!);
    const apart = a.x + a.w / 2 <= b.x - b.w / 2 || b.x + b.w / 2 <= a.x - a.w / 2;
    expect(apart).toBe(true);
    expect(placed.b!.x).not.toBe(140); // nor on the box
  });

  it("remembers moved boxes per tenant, and copes without storage", () => {
    savePositions("t1", { File: { x: 1, y: 2 } });
    expect(loadPositions("t1")).toEqual({ File: { x: 1, y: 2 } });
    expect(loadPositions("t2")).toEqual({});
    savePositions("t1", {});
    expect(loadPositions("t1")).toEqual({});
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(loadPositions("t1")).toEqual({});
    spy.mockRestore();
  });
});

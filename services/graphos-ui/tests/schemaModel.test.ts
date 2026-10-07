import type { GraphMap, TenantSchema } from "../src/api/types";
import {
  LABEL_NAME,
  labelTree,
  propertiesByLabel,
  relationshipRows,
  SEARCH_INDEX_NAME,
} from "../src/views/schema/schemaModel";

const schema: TenantSchema = {
  name: "t",
  vertex_labels: ["Symbol", "Function", "Class", "File", "Test"],
  edge_labels: ["CALLS", "DEFINES", "LINKS"],
  label_hierarchy: { Function: "Symbol", Class: "Symbol", Test: "Function" },
  search_indexes: {},
  rule_edge_labels: ["LINKS"],
};

describe("schema view", () => {
  it("shows labels as a tree under their parents", () => {
    const rows = labelTree(schema, new Map([["Function", 10]]), new Map([["Function", ["name"]]]));
    expect(rows.map((r) => `${"  ".repeat(r.depth)}${r.label}`)).toEqual([
      "File",
      "Symbol",
      "  Class",
      "  Function",
      "    Test",
    ]);
    const fn = rows.find((r) => r.label === "Function")!;
    expect([fn.parent, fn.count, fn.properties]).toEqual(["Symbol", 10, ["name"]]);
    expect(rows.find((r) => r.label === "File")!.count).toBe(0);
    expect(labelTree(schema, null, new Map())[0]!.count).toBeNull();
  });

  it("survives a cycle in the hierarchy", () => {
    const looped = { ...schema, vertex_labels: ["A", "B"], label_hierarchy: { A: "B", B: "A" } };
    expect(
      labelTree(looped, null, new Map())
        .map((r) => r.label)
        .sort(),
    ).toEqual(["A", "B"]);
  });

  it("inverts the catalogue into properties per label", () => {
    const byLabel = propertiesByLabel({ name: ["Function", "Class"], lines: ["Function"] });
    expect(byLabel.get("Function")).toEqual(["lines", "name"]);
    expect(byLabel.get("Class")).toEqual(["name"]);
  });

  it("counts relationship types and their label pairs, marking derived ones", () => {
    const map: GraphMap = {
      estimated: true,
      labels: [],
      links: [
        { src_label: "Function", type: "CALLS", dst_label: "Function", count: 90, estimated: true },
        { src_label: "Test", type: "CALLS", dst_label: "Function", count: 10, estimated: false },
        { src_label: "Class", type: "DEFINES", dst_label: "Function", count: 40, estimated: false },
      ],
    };
    const rows = relationshipRows(schema, map);
    expect(rows.map((r) => [r.type, r.count, r.estimated, r.derived])).toEqual([
      ["CALLS", 100, true, false],
      ["DEFINES", 40, false, false],
      ["LINKS", 0, false, true],
    ]);
    expect(rows[0]!.pairs[0]).toEqual({ src: "Function", dst: "Function", count: 90 });
  });

  it("checks names as graphos does", () => {
    expect(LABEL_NAME.test("Invoice")).toBe(true);
    expect(LABEL_NAME.test("invoice")).toBe(false);
    expect(SEARCH_INDEX_NAME.test("by_name")).toBe(true);
    expect(SEARCH_INDEX_NAME.test("ByName")).toBe(false);
  });
});

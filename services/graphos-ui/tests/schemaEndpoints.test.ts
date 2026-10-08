import type { GraphMap, TenantSchema } from "../src/api/types";
import { buildDiagram } from "../src/views/schema/diagram/diagramModel";
import { Endpoints, lineage, undeclaredInData } from "../src/lib/endpoints";
import { relationshipRows } from "../src/views/schema/schemaModel";

const schema: TenantSchema = {
  name: "t",
  vertex_labels: ["Repo", "File", "Symbol", "Function", "Class"],
  edge_labels: ["DEFINES", "CALLS", "LINKS"],
  label_hierarchy: { Function: "Symbol", Class: "Symbol" },
  search_indexes: {},
  rule_edge_labels: [],
  edge_endpoints: {
    DEFINES: [{ src: "File", dst: "Symbol" }],
    CALLS: [{ src: "Function", dst: "Function" }],
  },
  strict_endpoints: false,
};

const map: GraphMap = {
  labels: ["Repo", "File", "Symbol", "Function", "Class"].map((label) => ({ label, count: 3 })),
  links: [
    { src_label: "File", type: "DEFINES", dst_label: "Class", count: 40, estimated: false },
    { src_label: "Repo", type: "DEFINES", dst_label: "Class", count: 2, estimated: false },
    { src_label: "Repo", type: "LINKS", dst_label: "File", count: 9, estimated: false },
  ],
  estimated: false,
};

describe("declared endpoints", () => {
  it("checks a pair as graphos does: declared, through a parent, outside, or open", () => {
    const endpoints = new Endpoints(schema);
    expect(endpoints.any).toBe(true);
    expect(endpoints.check("DEFINES", "File", "Symbol")).toEqual({
      status: "declared",
      via: { src: "File", dst: "Symbol" },
    });
    expect(endpoints.check("DEFINES", "File", "Class").status).toBe("declared");
    expect(endpoints.exact("DEFINES", "File", "Class")).toBe(false);
    expect(endpoints.check("DEFINES", "Repo", "Class")).toEqual({ status: "undeclared", via: null });
    expect(endpoints.check("CALLS", "Symbol", "Function").status).toBe("undeclared");
    expect(endpoints.check("LINKS", "Repo", "File")).toEqual({ status: "open", via: null });
    expect(new Endpoints({ label_hierarchy: {} }).any).toBe(false);
  });

  it("tells which types a label may start, for the new-edge picker", () => {
    const endpoints = new Endpoints(schema);
    expect(endpoints.startsFrom("DEFINES", "File")).toBe(true);
    expect(endpoints.startsFrom("CALLS", "Function")).toBe(true);
    expect(endpoints.startsFrom("CALLS", "Class")).toBe(false);
    expect(endpoints.startsFrom("LINKS", "Repo")).toBe(true); // declares none: open
  });

  it("follows parents without looping", () => {
    expect(lineage("Function", schema.label_hierarchy)).toEqual(["Function", "Symbol"]);
    expect(lineage("A", { A: "B", B: "A" })).toEqual(["A", "B"]);
  });

  it("lists the pairs in the data that strict mode would refuse", () => {
    expect(undeclaredInData(schema, map)).toEqual([{ type: "DEFINES", src: "Repo", dst: "Class", count: 2 }]);
  });

  it("draws declared pairs, even without edges, and marks every line", () => {
    const diagram = buildDiagram(schema, map);
    const line = (key: string) => diagram.links.find((l) => l.key === key)!;
    expect(line("rel|File|Class")).toMatchObject({ status: "declared" });
    expect(line("rel|File|Class").types[0]).toMatchObject({ via: { src: "File", dst: "Symbol" } });
    expect(line("rel|Repo|Class").status).toBe("undeclared");
    expect(line("rel|Repo|File").status).toBe("open");
    // Declared, no edge yet: part of the definition all the same.
    expect(line("rel|File|Symbol").types).toEqual([
      { type: "DEFINES", count: 0, estimated: false, status: "declared", via: { src: "File", dst: "Symbol" } },
    ]);
    expect(line("rel|Function|Function").status).toBe("declared");
  });

  it("lists declared pairs and those outside them per relationship type", () => {
    const rows = relationshipRows(schema, map);
    const defines = rows.find((r) => r.type === "DEFINES")!;
    expect(defines.declared).toEqual([{ src: "File", dst: "Symbol" }]);
    expect(defines.undeclared).toBe(1);
    expect(rows.find((r) => r.type === "LINKS")).toMatchObject({ declared: [], undeclared: 0 });
  });
});

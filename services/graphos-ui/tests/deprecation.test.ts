import type { TenantSchema } from "../src/api/types";
import {
  deprecatedProperties,
  isDeprecated,
  nameableProperties,
  renamesNewestFirst,
  withDeprecation,
} from "../src/lib/deprecation";
import { jobName } from "../src/lib/jobs";

const SCHEMA: TenantSchema = {
  name: "acme",
  vertex_labels: ["Person", "Docu"],
  edge_labels: ["WROTE"],
  label_hierarchy: {},
  search_indexes: {},
  rule_edge_labels: [],
  property_schema: { vertex: { Person: { properties: { email: { type: "string" } } } } },
  deprecated: {
    vertex: {
      Docu: { deprecated: true, replaced_by: "Person", note: null, properties: {} },
      Person: { deprecated: false, replaced_by: null, note: null, properties: { mail: { replaced_by: "email" } } },
    },
  },
  renames: [
    { kind: "label", from: "Doc", to: "Docu", at: "2026-10-01T10:00:00Z", status: "done" },
    { kind: "property", from: "nm", to: "name", on: "Person", at: "2026-10-02T10:00:00Z", status: "running" },
  ],
};

describe("deprecations", () => {
  it("reads what is deprecated on a label or type", () => {
    expect(isDeprecated(SCHEMA, "vertex", "Docu")).toBe(true);
    expect(isDeprecated(SCHEMA, "vertex", "Person")).toBe(false);
    expect(deprecatedProperties(SCHEMA, "vertex", "Person")).toEqual([["mail", { replaced_by: "email" }]]);
  });

  it("deprecates the label itself or one property, keeping the rest of the entry", () => {
    const person = SCHEMA.deprecated!.vertex!.Person;
    expect(withDeprecation(person, null, { replaced_by: " Docu ", note: "" })).toEqual({
      deprecated: true,
      replaced_by: "Docu",
      properties: { mail: { replaced_by: "email" } },
    });
    expect(withDeprecation(person, "phone", { note: "until 2027" })).toEqual({
      properties: { mail: { replaced_by: "email" }, phone: { note: "until 2027" } },
    });
    expect(withDeprecation(undefined, "phone", {})).toEqual({ properties: { phone: {} } });
  });

  it("takes a mark back, and removes an entry left with nothing", () => {
    const docu = SCHEMA.deprecated!.vertex!.Docu;
    expect(withDeprecation(docu, null, null)).toBeNull();
    expect(withDeprecation(SCHEMA.deprecated!.vertex!.Person, "mail", null)).toBeNull();
    expect(withDeprecation({ ...docu, properties: { x: {} } }, null, null)).toEqual({ properties: { x: {} } });
  });

  it("lists the properties a rename can name, without the ones graphos writes", () => {
    expect(nameableProperties(SCHEMA, "vertex", "Person", ["name", "graphos_degree", "id"])).toEqual([
      "email",
      "mail",
      "name",
    ]);
  });

  it("lists the renames newest first", () => {
    expect(renamesNewestFirst(SCHEMA).map((r) => r.to)).toEqual(["name", "Docu"]);
  });

  it("names a property rename job", () => {
    expect(jobName("rename_property")).toBe("Property rename");
  });
});

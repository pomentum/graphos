import { beforeEach, describe, expect, it } from "vitest";
import type { TenantSchema } from "../src/api/types";
import { inverseOf, schemaUndo, UNDO_LIMIT, type UndoStep } from "../src/data/schemaUndo";

const before: TenantSchema = {
  name: "demo",
  vertex_labels: ["Person", "Team"],
  edge_labels: ["MEMBER_OF"],
  label_hierarchy: {},
  search_indexes: { people: { labels: ["Person"], properties: ["name"], analyzer: "text", language: "english" } },
  rule_edge_labels: [],
  edge_endpoints: { MEMBER_OF: [{ src: "Person", dst: "Team" }] },
  strict_endpoints: false,
  display: { vertex: { Person: { color: "#123456" } } },
  property_schema: { mode: "warn", vertex: { Person: { properties: { name: { type: "string" } } } } },
  deprecated: {},
};

const step = (inverse: ReturnType<typeof inverseOf>): UndoStep => {
  if ("irreversible" in inverse) throw new Error(`irreversible: ${inverse.irreversible.key}`);
  return inverse;
};

describe("inverseOf", () => {
  it("puts a style back as it was, and removes one that was new", () => {
    const changed = step(
      inverseOf(before, { kind: "add", addition: { display: { vertex: { Person: { color: "#ff0000" } } } } }),
    );
    expect(changed.label).toEqual({ key: "style", params: { name: "Person" } });
    expect(changed.ops).toEqual([
      { kind: "add", addition: { display: { vertex: { Person: { color: "#123456" } } }, display_merge: "entries" } },
    ]);
    const added = step(
      inverseOf(before, { kind: "add", addition: { display: { vertex: { Team: { color: "#00ff00" } } } } }),
    );
    expect(added.ops[0]).toEqual({
      kind: "add",
      addition: { display: { vertex: { Team: null } }, display_merge: "entries" },
    });
  });

  it("restores property definitions and the checking mode", () => {
    const inverse = step(
      inverseOf(before, {
        kind: "add",
        addition: { property_schema: { mode: "strict", vertex: { Person: null, Team: { properties: {} } } } },
      }),
    );
    expect(inverse.label).toEqual({ key: "change" });
    expect(inverse.ops).toEqual([
      {
        kind: "add",
        addition: {
          property_schema: {
            mode: "warn",
            vertex: { Person: { properties: { name: { type: "string" } } }, Team: null },
          },
        },
      },
    ]);
  });

  it("takes back a deprecation and the strict pairs switch", () => {
    const deprecation = step(
      inverseOf(before, { kind: "add", addition: { deprecated: { vertex: { Team: { deprecated: true } } } } }),
    );
    expect(deprecation.label).toEqual({ key: "deprecation", params: { name: "Team" } });
    expect(deprecation.ops).toEqual([{ kind: "add", addition: { deprecated: { vertex: { Team: null } } } }]);
    const strict = step(inverseOf(before, { kind: "add", addition: { strict_endpoints: true } }));
    expect(strict.ops).toEqual([{ kind: "add", addition: { strict_endpoints: false } }]);
  });

  it("removes a newly declared pair, and declares a removed one again", () => {
    const added = step(
      inverseOf(before, {
        kind: "add",
        addition: {
          edge_endpoints: {
            MEMBER_OF: [
              ["Person", "Team"],
              ["Team", "Team"],
            ],
          },
        },
      }),
    );
    // The pair that was already declared stays.
    expect(added.ops).toEqual([{ kind: "removeEndpoint", type: "MEMBER_OF", src: "Team", dst: "Team" }]);
    expect(added.label).toEqual({ key: "endpointAdded", params: { type: "MEMBER_OF", src: "Team", dst: "Team" } });
    const removed = step(inverseOf(before, { kind: "removeEndpoint", type: "MEMBER_OF", src: "Person", dst: "Team" }));
    expect(removed.ops).toEqual([{ kind: "add", addition: { edge_endpoints: { MEMBER_OF: [["Person", "Team"]] } } }]);
  });

  it("deprecates and drops a new label or type, its pairs going with the type", () => {
    const label = step(inverseOf(before, { kind: "add", addition: { vertex_labels: ["Project"] } }));
    expect(label.label).toEqual({ key: "newLabel", params: { name: "Project" } });
    expect(label.ops).toEqual([
      { kind: "add", addition: { deprecated: { vertex: { Project: { deprecated: true } } } } },
      { kind: "drop", name: "Project" },
    ]);
    const type = step(
      inverseOf(before, {
        kind: "add",
        addition: { edge_labels: ["WORKS_ON"], edge_endpoints: { WORKS_ON: [["Person", "Team"]] } },
      }),
    );
    expect(type.label).toEqual({ key: "newType", params: { name: "WORKS_ON" } });
    expect(type.ops).toEqual([
      { kind: "add", addition: { deprecated: { edge: { WORKS_ON: { deprecated: true } } } } },
      { kind: "drop", name: "WORKS_ON" },
    ]);
  });

  it("renames back, on the same label for a property", () => {
    const inverse = step(
      inverseOf(before, { kind: "rename", request: { kind: "property", from: "name", to: "full_name", on: "Person" } }),
    );
    expect(inverse.ops).toEqual([
      { kind: "rename", request: { kind: "property", from: "full_name", to: "name", on: "Person" } },
    ]);
  });

  it("says what cannot be undone", () => {
    expect(inverseOf(before, { kind: "add", addition: { label_hierarchy: { Team: "Person" } } })).toEqual({
      irreversible: { key: "parent", params: { name: "Team" } },
    });
    expect(
      inverseOf(before, {
        kind: "add",
        addition: {
          search_indexes: { teams: { labels: ["Team"], properties: ["name"], analyzer: "text", language: "english" } },
        },
      }),
    ).toEqual({ irreversible: { key: "searchIndex", params: { name: "teams" } } });
    expect(inverseOf(before, { kind: "drop", name: "Team" })).toEqual({
      irreversible: { key: "dropped", params: { name: "Team" } },
    });
    expect(inverseOf(null, { kind: "import" })).toEqual({ irreversible: { key: "imported" } });
    expect(inverseOf(null, { kind: "add", addition: { strict_endpoints: true } })).toEqual({
      irreversible: { key: "change" },
    });
  });

  it("puts a search index declared again back as it was", () => {
    const inverse = step(
      inverseOf(before, {
        kind: "add",
        addition: {
          search_indexes: {
            people: { labels: ["Person"], properties: ["name", "email"], analyzer: "text", language: "english" },
          },
        },
      }),
    );
    expect(inverse.ops).toEqual([
      { kind: "add", addition: { search_indexes: { people: before.search_indexes.people } } },
    ]);
  });
});

describe("schemaUndo", () => {
  const one: UndoStep = {
    label: { key: "strictEndpoints" },
    ops: [{ kind: "add", addition: { strict_endpoints: false } }],
  };
  beforeEach(() => {
    schemaUndo.clear("a");
    schemaUndo.clear("b");
  });

  it("keeps a stack per tenant, last in first out", () => {
    schemaUndo.record("a", one);
    schemaUndo.record("a", { ...one, label: { key: "change" } });
    expect(schemaUndo.get("b").steps).toEqual([]);
    expect(schemaUndo.pop("a")?.label.key).toBe("change");
    expect(schemaUndo.pop("a")?.label.key).toBe("strictEndpoints");
    expect(schemaUndo.pop("a")).toBeNull();
  });

  it("is emptied by a change that cannot be undone, and says which", () => {
    schemaUndo.record("a", one);
    schemaUndo.record("a", { irreversible: { key: "imported" } });
    expect(schemaUndo.get("a")).toEqual({ steps: [], blocked: { key: "imported" } });
    schemaUndo.record("a", one);
    expect(schemaUndo.get("a").blocked).toBeNull();
  });

  it("keeps the latest steps only", () => {
    for (let i = 0; i < UNDO_LIMIT + 5; i++)
      schemaUndo.record("a", { ...one, label: { key: "style", params: { name: `L${i}` } } });
    const { steps } = schemaUndo.get("a");
    expect(steps).toHaveLength(UNDO_LIMIT);
    expect(steps.at(-1)?.label.params?.name).toBe(`L${UNDO_LIMIT + 4}`);
  });

  it("tells its listeners", () => {
    let calls = 0;
    const stop = schemaUndo.subscribe(() => calls++);
    schemaUndo.record("a", one);
    schemaUndo.pop("a");
    stop();
    schemaUndo.record("a", one);
    expect(calls).toBe(2);
  });
});

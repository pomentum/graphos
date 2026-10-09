import type { ChangeItem } from "../src/api/types";
import { changeCategory, describeChange, openableNode, streamStatusText } from "../src/lib/changes";

const change = (extra: Partial<ChangeItem> = {}): ChangeItem => ({
  cursor: "1.0",
  at: "",
  op: "patch",
  kind: "node",
  label: "Doc",
  src_label: null,
  dst_label: null,
  ids: ["d1"],
  edges: null,
  rev: 2,
  count: 1,
  consumer: "sam",
  ...extra,
});

describe("activity rows", () => {
  it("opens only a change to one node that still exists", () => {
    expect(openableNode(change())).toEqual({ id: "d1", label: "Doc" });
    expect(openableNode(change({ op: "delete" }))).toBeNull();
    expect(openableNode(change({ count: 240, ids: ["d1", "d2"] }))).toBeNull();
    expect(openableNode(change({ kind: "edge" }))).toBeNull();
    expect(openableNode(change({ label: null }))).toBeNull();
  });

  it("words the feed's state the same everywhere", () => {
    expect(streamStatusText("live")).toBe("Live");
    expect(streamStatusText("connecting")).toBe("Connecting…");
    expect(streamStatusText("retrying")).toBe("Reconnecting…");
  });

  it("sorts changes into writes, computations and schema", () => {
    expect(changeCategory(change())).toBe("writes");
    expect(changeCategory(change({ op: "metrics", count: 9 }))).toBe("computations");
    expect(changeCategory(change({ op: "schema", kind: "tenant" }))).toBe("schema");
    expect(changeCategory(change({ op: "future" as ChangeItem["op"] }))).toBeNull();
  });

  it("describes schema changes", () => {
    const schema = (detail: Record<string, unknown>) => change({ op: "schema", kind: "tenant", detail });
    expect(describeChange(schema({ registered: true, added: { vertex_labels: ["Doc"] } }))).toBe(
      "Registered the tenant",
    );
    expect(describeChange(schema({ unregistered: true }))).toBe("Unregistered the tenant");
    expect(
      describeChange(
        schema({
          added: { vertex_labels: ["A", "B", "C", "D", "E"], edge_endpoints: [["LINKS", "Doc", "Tag"]], indexes: [2] },
          removed: { label_hierarchy: [["B", "A"]] },
          strict_endpoints: [false, true],
        }),
      ),
    ).toBe(
      "Changed the schema: added labels A, B, C, 2 more, label pair Doc –LINKS→ Tag, 2 indexes; " +
        "removed parent B → A; strict endpoints on",
    );
    // Declared since 0.6.0: listed by what they are.
    expect(
      describeChange(
        schema({
          added: {
            property_indexes: ["Doc(kind)", "User(email) unique"],
            generated_columns: ["Doc.kind (text, from kind)"],
          },
        }),
      ),
    ).toBe(
      "Changed the schema: added property indexes Doc(kind), User(email) unique, " +
        "generated column Doc.kind (text, from kind)",
    );
    expect(describeChange(schema({ added: { generated_columns: [1] } }))).toBe(
      "Changed the schema: added 1 generated column",
    );
  });

  it("words a summary over every label without a label", () => {
    const summary = change({ op: "metrics", label: null, ids: null, count: 486950 });
    expect(describeChange(summary)).toBe("Measured 486,950 nodes");
    expect(describeChange(change({ op: "resolve", kind: "edge", label: null, ids: null, count: 1 }))).toBe(
      "Derived 1 edge",
    );
  });

  it("names an op it does not know as it is", () => {
    expect(describeChange(change({ op: "future" as ChangeItem["op"] }))).toBe("future Doc d1");
  });
});

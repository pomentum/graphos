import type { ChangeItem, Neighbor, TenantSchema } from "../src/api/types";
import { pickSearchIndex } from "../src/data/searchIndex";
import { describeChange } from "../src/lib/changes";
import { countByEdgeType } from "../src/lib/expand";
import { isEmptyPatch, mergePatch, propertyKind, sortedEntries } from "../src/lib/properties";
import { formatBytes, relativeTime } from "../src/lib/time";
import { pickTenant } from "../src/session/SessionProvider";
import { SessionStore } from "../src/session/sessionStore";
import { menuGroups, VIEWS, visibleViews, type ViewDefinition } from "../src/views/registry";

describe("mergePatch", () => {
  it("sends changed and new keys, and null for removed ones", () => {
    expect(mergePatch({ a: 1, b: "x", c: [1] }, { a: 2, c: [1], d: true })).toEqual({ a: 2, b: null, d: true });
    expect(isEmptyPatch(mergePatch({ a: { b: 1 } }, { a: { b: 1 } }))).toBe(true);
  });

  it("knows how each value is edited and lists system keys last", () => {
    expect([propertyKind(1), propertyKind(true), propertyKind({}), propertyKind("s"), propertyKind(null)]).toEqual([
      "number",
      "boolean",
      "json",
      "text",
      "text",
    ]);
    expect(sortedEntries({ id: "1", zeta: 1, alpha: 2 }).map(([k]) => k)).toEqual(["alpha", "zeta", "id"]);
  });
});

describe("expand", () => {
  const n = (type: string, direction: "in" | "out"): Neighbor => ({
    neighbor_id: "x",
    neighbor_label: "L",
    neighbor_properties: {},
    direction,
    edge_type: type,
    edge_properties: {},
  });

  it("counts neighbours per edge type, most first", () => {
    expect(countByEdgeType([n("A", "out"), n("B", "in"), n("B", "out")])).toEqual([
      { type: "B", out: 1, in: 1 },
      { type: "A", out: 1, in: 0 },
    ]);
  });
});

describe("describeChange", () => {
  const base: ChangeItem = {
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
  };
  it("names one node, or counts many", () => {
    expect(describeChange(base)).toBe("Edited Doc d1");
    expect(describeChange({ ...base, op: "upsert", ids: null, count: 240 })).toBe("Wrote 240 Doc nodes");
    expect(describeChange({ ...base, kind: "edge", op: "resolve", label: "KNOWS", count: 12 })).toBe(
      "Derived 12 KNOWS edges",
    );
  });
});

describe("time and size", () => {
  it("says how long ago", () => {
    const now = new Date("2026-10-07T12:00:00Z");
    expect(relativeTime("2026-10-07T11:59:40Z", now)).toBe("just now");
    expect(relativeTime("2026-10-07T11:55:00Z", now)).toMatch(/5 min/);
    expect(relativeTime("2026-10-06T12:00:00Z", now)).toBe("yesterday");
  });
  it("formats sizes", () => {
    expect([formatBytes(512), formatBytes(1_500_000), formatBytes(25_000_000)]).toEqual(["512 B", "1.5 MB", "25 MB"]);
  });
});

describe("pickSearchIndex", () => {
  const schema = (indexes: TenantSchema["search_indexes"]): TenantSchema => ({
    name: "t",
    vertex_labels: [],
    edge_labels: [],
    label_hierarchy: {},
    search_indexes: indexes,
    rule_edge_labels: [],
  });
  it("prefers an identifier index over every label", () => {
    expect(
      pickSearchIndex(
        schema({
          prose: { labels: ["*"], properties: ["s"], analyzer: "text", language: "english" },
          classes: { labels: ["Class"], properties: ["name"], analyzer: "identifier", language: "simple" },
          names: { labels: ["*"], properties: ["name"], analyzer: "identifier", language: "simple" },
        }),
      ),
    ).toBe("names");
    expect(pickSearchIndex(schema({}))).toBeUndefined();
  });
});

describe("session", () => {
  const me = (tenants: string[]) => ({ consumer: "c", admin: false, access: "read" as const, tenants });
  it("picks the remembered tenant, or the only one", () => {
    expect(pickTenant(me(["a", "b"]), "b")).toBe("b");
    expect(pickTenant(me(["a", "b"]), "gone")).toBeNull();
    expect(pickTenant(me(["a"]), null)).toBe("a");
  });

  it("stores the session for the tab and survives broken storage", () => {
    const data = new Map<string, string>();
    const storage = {
      getItem: (k: string) => data.get(k) ?? null,
      setItem: (k: string, v: string) => void data.set(k, v),
      removeItem: (k: string) => void data.delete(k),
    } as Storage;
    const store = new SessionStore(storage);
    store.save({ token: "t", tenant: "a" });
    expect(store.load()).toEqual({ token: "t", tenant: "a" });
    store.clear();
    expect(store.load()).toBeNull();
    data.set("graphos.session", "{not json");
    expect(store.load()).toBeNull();
    expect(new SessionStore(null).load()).toBeNull();
  });
});

describe("view registry", () => {
  it("hides admin views from other tokens and groups the menu", () => {
    const admin = { ...VIEWS[0]!, id: "tenants", group: "Admin", requires: "admin" } as ViewDefinition;
    expect(visibleViews({ isAdmin: false }, [...VIEWS, admin]).map((v) => v.id)).not.toContain("tenants");
    expect(visibleViews({ isAdmin: true }, [...VIEWS, admin]).map((v) => v.id)).toContain("tenants");
    expect(menuGroups(VIEWS).map(([group]) => group)).toEqual(["Graph", "Insights", "Data"]);
  });
});

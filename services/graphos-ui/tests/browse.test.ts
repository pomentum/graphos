import { currentCursor, FIRST_PAGE, nextPage, previousPage } from "../src/lib/pager";
import { profileProperties, valueLike } from "../src/lib/properties";
import { browseParams, defaultColumns, readBrowseState, scanFilter } from "../src/views/browse/browseState";

describe("cursor pager", () => {
  it("remembers the cursors seen so it can go back", () => {
    const second = nextPage(FIRST_PAGE, "c1");
    const third = nextPage(second, "c2");
    expect(currentCursor(FIRST_PAGE)).toBeNull();
    expect(currentCursor(third)).toBe("c2");
    expect(currentCursor(previousPage(third))).toBe("c1");
    expect(previousPage(FIRST_PAGE)).toBe(FIRST_PAGE);
    // Going forward again after going back replaces what followed.
    expect(nextPage(previousPage(third), "c9").cursors).toEqual([null, "c1", "c9"]);
  });
});

describe("property profile", () => {
  const nodes = [
    { properties: { id: "a", name: "x", public: true } },
    { properties: { id: "b", name: null, lines: 3 } },
    { properties: { id: "c", name: "z" } },
  ];

  it("ranks properties by how many nodes have them, with a non-null example", () => {
    const profile = profileProperties(nodes);
    expect(profile.map((p) => [p.name, p.count])).toEqual([
      ["id", 3],
      ["name", 3],
      ["lines", 1],
      ["public", 1],
    ]);
    expect(profile.find((p) => p.name === "name")?.example).toBe("x");
    expect(defaultColumns(profile)).toEqual(["name", "lines", "public"]);
  });

  it("types filter text like the property's values", () => {
    expect(valueLike("42", 7)).toBe(42);
    expect(valueLike("abc", 7)).toBe("abc");
    expect(valueLike("", 7)).toBe("");
    expect(valueLike("true", false)).toBe(true);
    expect(valueLike("0042", "id")).toBe("0042");
    const profile = profileProperties(nodes);
    expect(scanFilter({ property: "public", op: "=", value: "true" }, profile)).toEqual({
      property: "public",
      op: "=",
      value: true,
    });
    expect(scanFilter({ property: "lines", op: "exists", value: "ignored" }, profile)).toEqual({
      property: "lines",
      op: "exists",
    });
  });
});

describe("browse address", () => {
  it("round-trips label and filter through the query string", () => {
    const state = { label: "Function", filter: { property: "public", op: "!=" as const, value: "true" } };
    expect(readBrowseState(browseParams(state))).toEqual(state);
    const unset = { label: "Doc", filter: { property: "owner", op: "not exists" as const, value: "" } };
    expect(browseParams(unset).toString()).toBe("label=Doc&p=owner&op=unset");
    expect(readBrowseState(browseParams(unset))).toEqual(unset);
  });

  it("ignores a malformed filter", () => {
    expect(readBrowseState(new URLSearchParams("label=Doc&p=x&op=like"))).toEqual({ label: "Doc", filter: null });
    expect(readBrowseState(new URLSearchParams(""))).toEqual({ label: null, filter: null });
  });
});

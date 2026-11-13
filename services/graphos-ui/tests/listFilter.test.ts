import { describe, expect, it } from "vitest";
import { matches } from "../src/components/ListFilter";

describe("matches", () => {
  it("finds the filter in any of the texts, in any case; an empty filter matches all", () => {
    expect(matches("", "anything")).toBe(true);
    expect(matches("  ", null)).toBe(true);
    expect(matches("per", "Person")).toBe(true);
    expect(matches("PER", undefined, "super")).toBe(true);
    expect(matches("team", "Person", null)).toBe(false);
  });
});

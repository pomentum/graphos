import { layoutDuration } from "../src/graph/layout";
import { isApple, searchShortcut } from "../src/lib/platform";

describe("platform shortcuts", () => {
  it("writes the search shortcut the way each platform does", () => {
    expect(searchShortcut({ platform: "Win32", userAgent: "" })).toBe("/");
    expect(searchShortcut({ platform: "Linux x86_64", userAgent: "" })).toBe("/");
    expect(searchShortcut({ platform: "MacIntel", userAgent: "" })).toBe("⌘K");
    expect(isApple({ platform: "", userAgent: "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)" })).toBe(true);
  });
});

describe("layout duration", () => {
  it("grows with the graph and is capped", () => {
    expect(layoutDuration(10)).toBeLessThan(layoutDuration(2_000));
    expect(layoutDuration(100_000)).toBe(8_000);
  });
});

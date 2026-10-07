import { middleEllipsis } from "../src/lib/text";

describe("middleEllipsis", () => {
  it("keeps short text and cuts long text in the middle, keeping both ends", () => {
    expect(middleEllipsis("short", 10)).toBe("short");
    const cut = middleEllipsis("repo/src/main/java/com/example/billing/Invoice.java", 24);
    expect(cut).toHaveLength(24);
    expect(cut.startsWith("repo/src/")).toBe(true);
    expect(cut.endsWith("Invoice.java")).toBe(true);
    expect(cut).toContain("…");
  });
});

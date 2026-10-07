import { DEFAULT_HOPS, pathParams, readPathState, type PathState } from "../src/views/path/pathState";

describe("path address", () => {
  it("round-trips ends (ids with colons too), direction, hops and types", () => {
    const state: PathState = {
      from: { id: "fn:billing.Invoice", label: "Function", name: "Invoice" },
      to: { id: "c1", label: "Class", name: "Cart" },
      direction: "out",
      maxHops: 6,
      relTypes: ["CALLS", "USES"],
    };
    expect(readPathState(pathParams(state))).toEqual(state);
  });

  it("leaves defaults out and ignores what it cannot read", () => {
    const empty: PathState = { from: null, to: null, direction: "both", maxHops: DEFAULT_HOPS, relTypes: [] };
    expect(pathParams(empty).toString()).toBe("");
    expect(readPathState(new URLSearchParams("from=nolabel&dir=up&hops=99"))).toEqual(empty);
    expect(readPathState(new URLSearchParams("from=Doc:d1")).from).toEqual({ id: "d1", label: "Doc", name: "d1" });
  });
});

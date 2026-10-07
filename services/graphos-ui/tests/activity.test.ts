import type { ChangeItem } from "../src/api/types";
import { openableNode, streamStatusText } from "../src/lib/changes";

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
});

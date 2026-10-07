import type { ChangeItem } from "../api/types";

const VERBS: Record<ChangeItem["op"], string> = {
  upsert: "wrote",
  merge: "updated",
  patch: "edited",
  delete: "deleted",
  resolve: "derived",
  communities: "regrouped",
};

/** One line for a change: "edited Person u1", "wrote 240 Person nodes", "derived 12 KNOWS edges". */
export function describeChange(change: ChangeItem): string {
  const verb = VERBS[change.op];
  const what = change.label ?? (change.kind === "node" ? "nodes" : "edges");
  const ids = change.kind === "node" ? (change.ids ?? []) : [];
  if (change.count === 1 && ids.length === 1) return `${capitalise(verb)} ${what} ${ids[0]}`;
  const noun = change.kind === "node" ? "node" : "edge";
  return `${capitalise(verb)} ${change.count.toLocaleString("en")} ${what} ${noun}${change.count === 1 ? "" : "s"}`;
}

function capitalise(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

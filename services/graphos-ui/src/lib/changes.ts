import type { StreamStatus } from "../api/changeStream";
import type { ChangeItem } from "../api/types";
import { t } from "../i18n";
import { formatCount } from "./time";

/** The feed's state in words, the same wherever it is shown. */
export function streamStatusText(status: StreamStatus): string {
  return status === "live"
    ? t("common.stream.live")
    : status === "retrying"
      ? t("common.stream.retrying")
      : t("common.stream.connecting");
}

/** The node a change can open on the canvas: a change to one node that still exists. */
export function openableNode(change: ChangeItem): { id: string; label: string } | null {
  const id = change.ids?.[0];
  if (change.kind !== "node" || change.op === "delete" || !id || !change.label || change.count !== 1) return null;
  return { id, label: change.label };
}

/** One line for a change: "Edited Person u1", "Wrote 240 Person nodes", "Derived 12 KNOWS edges". */
export function describeChange(change: ChangeItem): string {
  const verb = t(`common.changes.verbs.${change.op}`);
  const node = change.kind === "node";
  const what = change.label ?? (node ? t("common.changes.anyNodes") : t("common.changes.anyEdges"));
  const ids = node ? (change.ids ?? []) : [];
  if (change.count === 1 && ids.length === 1) return t("common.changes.one", { verb, what, id: ids[0] });
  const values = { verb, what, count: change.count, formatted: formatCount(change.count) };
  return node ? t("common.changes.nodes", values) : t("common.changes.edges", values);
}

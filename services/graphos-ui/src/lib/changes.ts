import type { StreamStatus } from "../api/changeStream";
import type { ChangeItem, SchemaChangeDetail } from "../api/types";
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

/** What the Activity view can show on its own: writes, computations, schema changes. */
export type ActivityCategory = "writes" | "computations" | "schema";

/** The change-feed ops of each category (what the history is read with). */
export const CATEGORY_OPS: Record<ActivityCategory, ChangeItem["op"][]> = {
  writes: ["upsert", "merge", "patch", "delete"],
  computations: ["resolve", "communities", "metrics"],
  schema: ["schema"],
};

const VERBS: ChangeItem["op"][] = Object.values(CATEGORY_OPS).flat();

/** The category of a change; null for an op this UI does not know yet. */
export function changeCategory(change: ChangeItem): ActivityCategory | null {
  for (const [category, ops] of Object.entries(CATEGORY_OPS) as [ActivityCategory, ChangeItem["op"][]][]) {
    if (ops.includes(change.op)) return category;
  }
  return null;
}

/** One line for a change: "Edited Person u1", "Wrote 240 Person nodes", "Derived 12 KNOWS edges". */
export function describeChange(change: ChangeItem): string {
  if (change.op === "schema") return describeSchema((change.detail ?? {}) as SchemaChangeDetail);
  // A newer graphos may record ops this UI does not know: name them as they are.
  const verb = VERBS.includes(change.op) ? t(`common.changes.verbs.${change.op}`) : String(change.op);
  const node = change.kind === "node";
  const ids = node ? (change.ids ?? []) : [];
  if (change.count === 1 && ids.length === 1) {
    const what = change.label ?? t("common.changes.anyNode");
    return t("common.changes.one", { verb, what, id: ids[0] });
  }
  const values = { verb, what: change.label ?? "", count: change.count, formatted: formatCount(change.count) };
  // A summary over several labels (a whole-graph computation) names none.
  if (!change.label) return node ? t("common.changes.nodesAny", values) : t("common.changes.edgesAny", values);
  return node ? t("common.changes.nodes", values) : t("common.changes.edges", values);
}

type SchemaField =
  | "vertex_labels"
  | "edge_labels"
  | "edge_endpoints"
  | "label_hierarchy"
  | "search_indexes"
  | "resolve_rules"
  | "property_indexes"
  | "generated_columns"
  | "meta"
  | "display"
  | "property_schema"
  | "indexes";

const SCHEMA_FIELDS: SchemaField[] = [
  "vertex_labels",
  "edge_labels",
  "edge_endpoints",
  "label_hierarchy",
  "search_indexes",
  "resolve_rules",
  "property_indexes",
  "generated_columns",
  "meta",
  "display",
  "property_schema",
  "indexes",
];

/** Fields whose items are entries `vertex:<label>` / `edge:<type>` (`*`: every one). */
const BY_ENTRY: SchemaField[] = ["display", "property_schema"];

/** "Person", "AUTHORED", "every label": one entry of a field merged by entry. */
function entryName(entry: string): string {
  const [part, name] = entry.split(":", 2);
  if (name !== "*") return name ?? entry;
  return part === "edge" ? t("common.changes.schema.everyType") : t("common.changes.schema.everyLabel");
}

/** How many names a schema change lists before "N more". */
const NAMES_SHOWN = 3;

/** "labels Tag, Doc", "label pair Doc –LINKS→ Tag", "property index Doc(kind)". */
function schemaItems(field: SchemaField, values: unknown[]): string {
  // Records from before 0.6.0, when these were SQL statements, hold only how many.
  if (field === "indexes" || (field === "generated_columns" && typeof values[0] === "number")) {
    const count = Number(values[0] ?? 0);
    return `${count} ${t(`common.changes.schema.fields.${field}`, { count })}`;
  }
  const names = values.map((value) => {
    if (BY_ENTRY.includes(field)) return entryName(String(value));
    if (!Array.isArray(value)) return String(value);
    if (field === "edge_endpoints") return `${value[1]} –${value[0]}→ ${value[2]}`;
    return value.join(" → "); // label_hierarchy: child → parent
  });
  const shown = names.slice(0, NAMES_SHOWN).join(", ");
  const rest = names.length - NAMES_SHOWN;
  const list = rest > 0 ? `${shown}, ${t("common.changes.schema.more", { count: rest })}` : shown;
  return `${t(`common.changes.schema.fields.${field}`, { count: names.length })} ${list}`;
}

/** One line for a schema change: "Changed the schema: added labels Tag, Doc; strict endpoints on". */
export function describeSchema(detail: SchemaChangeDetail): string {
  if (detail.data_deleted) return t("common.changes.schema.dataDeleted");
  if (detail.restored) return t("common.changes.schema.restored");
  if (detail.unregistered) return t("common.changes.schema.unregistered");
  if (detail.registered) return t("common.changes.schema.registered");
  const parts: string[] = [];
  for (const side of ["added", "removed", "changed"] as const) {
    const fields = detail[side] ?? {};
    const items = SCHEMA_FIELDS.filter((field) => fields[field]?.length).map((field) =>
      schemaItems(field, fields[field]!),
    );
    // "changed" (meta notes rewritten) has its own wording: "Changed the schema" is the line itself.
    const key = side === "changed" ? "changedParts" : side;
    if (items.length) parts.push(t(`common.changes.schema.${key}`, { items: items.join(", ") }));
  }
  if (detail.strict_endpoints) {
    parts.push(detail.strict_endpoints[1] ? t("common.changes.schema.strictOn") : t("common.changes.schema.strictOff"));
  }
  if (detail.property_mode) {
    const [before, after] = detail.property_mode;
    parts.push(t("common.changes.schema.propertyMode", { before: before ?? "off", after }));
  }
  return parts.length
    ? t("common.changes.schema.changed", { parts: parts.join("; ") })
    : t("common.changes.verbs.schema");
}

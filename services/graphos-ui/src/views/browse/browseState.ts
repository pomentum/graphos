import type { ParseKeys } from "i18next";
import type { ScanFilter } from "../../api/types";
import { valueLike, type PropertyProfile } from "../../lib/properties";

export type FilterOp = ScanFilter["op"];

/** The filter as the user typed it; the value is typed only when the scan is sent. */
export interface BrowseFilter {
  property: string;
  op: FilterOp;
  value: string;
}

/** What Browse shows, kept in the address so it can be bookmarked and shared. */
export interface BrowseState {
  label: string | null;
  filter: BrowseFilter | null;
}

/** The operators offered, in order; `label` is an i18n key, translated where it is shown. */
export const FILTER_OPS: { value: FilterOp; label: ParseKeys }[] = [
  { value: "=", label: "views.browse.ops.is" },
  { value: "!=", label: "views.browse.ops.isNot" },
  { value: "exists", label: "views.browse.ops.isSet" },
  { value: "not exists", label: "views.browse.ops.isNotSet" },
];

/** Short, URL-friendly names for the operators. */
const OP_PARAM: Record<FilterOp, string> = { "=": "eq", "!=": "ne", exists: "set", "not exists": "unset" };

/** Columns shown before the user picks their own: the most common properties. */
const DEFAULT_COLUMNS = 5;

/** Whether the operator compares with a value (`is`) or only checks presence (`is set`). */
export function needsValue(op: FilterOp): boolean {
  return op === "=" || op === "!=";
}

/** Reads the state from the query string (`?label=…&p=…&op=…&v=…`); anything malformed is ignored. */
export function readBrowseState(params: URLSearchParams): BrowseState {
  const label = params.get("label") || null;
  const property = params.get("p");
  const op = (Object.keys(OP_PARAM) as FilterOp[]).find((key) => OP_PARAM[key] === params.get("op"));
  const filter = property && op ? { property, op, value: needsValue(op) ? (params.get("v") ?? "") : "" } : null;
  return { label, filter };
}

/** The query string for a state; the inverse of `readBrowseState`. */
export function browseParams({ label, filter }: BrowseState): URLSearchParams {
  const params = new URLSearchParams();
  if (label) params.set("label", label);
  if (filter) {
    params.set("p", filter.property);
    params.set("op", OP_PARAM[filter.op]);
    if (needsValue(filter.op)) params.set("v", filter.value);
  }
  return params;
}

/**
 * The filter as a scan condition. The typed value takes the type the
 * property has on the label's nodes, so `42` matches a number and `true`
 * a boolean, while ids such as `0042` on a text property stay text.
 */
export function scanFilter(filter: BrowseFilter, profile: PropertyProfile[]): ScanFilter {
  if (!needsValue(filter.op)) return { property: filter.property, op: filter.op };
  const example = profile.find((p) => p.name === filter.property)?.example;
  return { property: filter.property, op: filter.op, value: valueLike(filter.value, example) };
}

/** The most common properties after `id`, which always has its own column. */
export function defaultColumns(profile: PropertyProfile[]): string[] {
  return profile
    .map((p) => p.name)
    .filter((name) => name !== "id")
    .slice(0, DEFAULT_COLUMNS);
}

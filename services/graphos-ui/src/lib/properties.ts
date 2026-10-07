import type { Properties } from "../api/types";

/** Properties graphos manages itself; shown, never edited. */
export const SYSTEM_PROPERTIES = new Set(["id", "community_id"]);

/** How a property is edited: by its current value's type. */
export type PropertyKind = "text" | "number" | "boolean" | "json";

export function propertyKind(value: unknown): PropertyKind {
  if (typeof value === "number") return "number";
  if (typeof value === "boolean") return "boolean";
  if (value !== null && typeof value === "object") return "json";
  return "text";
}

/**
 * The JSON Merge Patch that turns `original` into `draft`: changed and new
 * keys with their values, removed keys as `null`. Empty when nothing changed.
 */
export function mergePatch(original: Properties, draft: Properties): Properties {
  const patch: Properties = {};
  for (const [key, value] of Object.entries(draft)) {
    if (!sameValue(original[key], value)) patch[key] = value;
  }
  for (const key of Object.keys(original)) {
    if (!(key in draft)) patch[key] = null;
  }
  return patch;
}

/** True when the patch changes nothing. */
export function isEmptyPatch(patch: Properties): boolean {
  return Object.keys(patch).length === 0;
}

/** Editable properties first in name order, system ones last. */
export function sortedEntries(properties: Properties): [string, unknown][] {
  return Object.entries(properties).sort(([a], [b]) => {
    const sa = SYSTEM_PROPERTIES.has(a) ? 1 : 0;
    const sb = SYSTEM_PROPERTIES.has(b) ? 1 : 0;
    return sa - sb || a.localeCompare(b);
  });
}

/** A value as one line of text, for read-only display. */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

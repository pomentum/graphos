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

/** How often a property appears in a sample of nodes, with one of its values. */
export interface PropertyProfile {
  name: string;
  /** Nodes of the sample that have it. */
  count: number;
  /** A non-null value seen for it, to tell numbers and booleans from text. */
  example: unknown;
}

/** The properties of a sample of nodes, most common first (ties by name). */
export function profileProperties(nodes: { properties: Properties }[]): PropertyProfile[] {
  const profiles = new Map<string, PropertyProfile>();
  for (const node of nodes) {
    for (const [name, value] of Object.entries(node.properties)) {
      const profile = profiles.get(name) ?? { name, count: 0, example: null };
      profile.count += 1;
      if (profile.example === null && value !== null) profile.example = value;
      profiles.set(name, profile);
    }
  }
  return [...profiles.values()].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
}

/**
 * Typed text as a value of the same type as `example`: a number or boolean
 * when the property holds those and the text reads as one, else the text.
 */
export function valueLike(text: string, example: unknown): unknown {
  const trimmed = text.trim();
  if (typeof example === "number" && trimmed !== "" && Number.isFinite(Number(trimmed))) return Number(trimmed);
  if (typeof example === "boolean" && (trimmed === "true" || trimmed === "false")) return trimmed === "true";
  return text;
}

/**
 * Typed text as a value for a new property: a number or a boolean when it
 * reads exactly as one (`42`, `-1.5`, `true`), else the text itself. `0042`
 * or `1e3` stay text, since they would not read back the same.
 */
export function typedValue(text: string): unknown {
  if (text === "true" || text === "false") return text === "true";
  const number = Number(text);
  if (text.trim() !== "" && Number.isFinite(number) && String(number) === text) return number;
  return text;
}

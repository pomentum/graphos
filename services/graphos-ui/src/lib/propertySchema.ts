import type { EffectivePolicy, PropertyEntry, PropertySchema, PropertySpec, Scalar, TenantSchema } from "../api/types";
import { t } from "../i18n";

/** Every label (or every relationship type), now and later. */
export const ALL = "*";

export type SchemaPart = "vertex" | "edge";

/** A property name graphos accepts (`contracts.property_schema`). */
export const PROPERTY_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,62}$/;

/** Values of an enum shown before "and N more". */
const ENUM_SHOWN = 4;

/** Names graphos writes itself: never declared, never undeclared (`graphos.reserved`). */
export function writtenByGraphos(name: string): boolean {
  return name === "id" || name.startsWith("graphos_") || name === "community_id";
}

/** The entry a label or type (or `*`) declares, if any. */
export function entryOf(schema: TenantSchema, part: SchemaPart, name: string): PropertyEntry | undefined {
  return (schema.property_schema as PropertySchema | undefined)?.[part]?.[name];
}

/** Whether an entry declares nothing at all (sent as `null`, it is removed). */
export function isEmptyEntry(entry: PropertyEntry): boolean {
  return !entry.description && !entry.mode && !entry.undeclared && Object.keys(entry.properties ?? {}).length === 0;
}

/** The entry with one property's definition set, or removed (`null`); `null` when nothing is left. */
export function withDefinition(
  entry: PropertyEntry | undefined,
  name: string,
  spec: PropertySpec | null,
): PropertyEntry | null {
  const properties = { ...(entry?.properties ?? {}) };
  if (spec) properties[name] = spec;
  else delete properties[name];
  const next: PropertyEntry = { ...entry, properties };
  return isEmptyEntry(next) ? null : next;
}

/** The entry with its own settings replaced (empty ones dropped); `null` when nothing is left. */
export function withSettings(
  entry: PropertyEntry | undefined,
  settings: Pick<PropertyEntry, "description" | "mode" | "undeclared">,
): PropertyEntry | null {
  const next: PropertyEntry = { properties: entry?.properties ?? {} };
  if (settings.description?.trim()) next.description = settings.description.trim();
  if (settings.mode) next.mode = settings.mode;
  if (settings.undeclared) next.undeclared = settings.undeclared;
  return isEmptyEntry(next) ? null : next;
}

/** "every label", "every relationship type", or the name itself. */
export function sourceName(part: SchemaPart, source: string): string {
  if (source !== ALL) return source;
  return part === "edge" ? t("common.changes.schema.everyType") : t("common.changes.schema.everyLabel");
}

function listed(values: Scalar[], full: boolean): string {
  if (full) return values.map(String).join(", ");
  const shown = values.slice(0, ENUM_SHOWN).map(String).join(", ");
  return values.length > ENUM_SHOWN
    ? `${shown} ${t("views.schema.properties.rules.more", { count: values.length - ENUM_SHOWN })}`
    : shown;
}

/**
 * A definition in words, one short phrase per keyword it states: "string",
 * "required", "one of lei, euid, other", "matches [A-Z]{2}", "0 to 1".
 */
export function rulesOf(spec: PropertySpec, full = false): string[] {
  const out: string[] = [];
  if (spec.type && spec.type !== "any") out.push(t(`views.schema.properties.rules.types.${spec.type}`));
  if (spec.required) out.push(t("views.schema.properties.rules.required"));
  else if (spec.recommended) out.push(t("views.schema.properties.rules.recommended"));
  if (spec.enum?.length === 1) out.push(t("views.schema.properties.rules.always", { value: String(spec.enum[0]) }));
  else if (spec.enum?.length) out.push(t("views.schema.properties.rules.oneOf", { values: listed(spec.enum, full) }));
  if (spec.pattern) out.push(t("views.schema.properties.rules.matches", { pattern: spec.pattern }));
  if (spec.format) out.push(t(`views.schema.properties.rules.formats.${spec.format}`));
  const { minimum: min, maximum: max } = spec;
  if (min !== undefined && max !== undefined) out.push(t("views.schema.properties.rules.between", { min, max }));
  else if (min !== undefined) out.push(t("views.schema.properties.rules.atLeast", { min }));
  else if (max !== undefined) out.push(t("views.schema.properties.rules.atMost", { max }));
  const items = spec.items;
  if (items) {
    const parts = [
      items.type && items.type !== "any" ? t(`views.schema.properties.rules.types.${items.type}`) : null,
      items.enum?.length ? t("views.schema.properties.rules.oneOf", { values: listed(items.enum, full) }) : null,
      items.pattern ? t("views.schema.properties.rules.matches", { pattern: items.pattern }) : null,
    ].filter(Boolean);
    if (parts.length) out.push(t("views.schema.properties.rules.items", { rules: parts.join(", ") }));
  }
  return out;
}

/** Property names the rows carry that no definition names (graphos's own left out), sorted. */
export function undeclaredNames(carried: Iterable<string>, policy: EffectivePolicy | undefined): string[] {
  const declared = policy?.properties ?? {};
  return [...new Set(carried)].filter((name) => !(name in declared) && !writtenByGraphos(name)).sort();
}

/**
 * Enum values typed as text, read as the property's type: numbers for
 * `number` / `integer`, `true` / `false` for `boolean`; text otherwise.
 * `null` when one does not read as the type.
 */
export function enumValues(texts: string[], type: PropertySpec["type"]): Scalar[] | null {
  const out: Scalar[] = [];
  for (const text of texts) {
    if (type === "number" || type === "integer") {
      const value = Number(text);
      if (text.trim() === "" || Number.isNaN(value) || (type === "integer" && !Number.isInteger(value))) return null;
      out.push(value);
    } else if (type === "boolean") {
      if (text !== "true" && text !== "false") return null;
      out.push(text === "true");
    } else {
      out.push(text);
    }
  }
  return out;
}

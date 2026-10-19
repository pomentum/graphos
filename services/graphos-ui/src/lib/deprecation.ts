import type { DeprecatedEntry, DeprecatedProperty, SchemaRename, TenantSchema } from "../api/types";
import { writtenByGraphos, type SchemaPart } from "./propertySchema";

/** What is deprecated on a label or type, if anything. */
export function deprecationOf(schema: TenantSchema, part: SchemaPart, name: string): DeprecatedEntry | undefined {
  return schema.deprecated?.[part]?.[name];
}

/** Whether the label or type itself is deprecated. */
export function isDeprecated(schema: TenantSchema, part: SchemaPart, name: string): boolean {
  return Boolean(deprecationOf(schema, part, name)?.deprecated);
}

/** A label's or type's deprecated properties, by name. */
export function deprecatedProperties(
  schema: TenantSchema,
  part: SchemaPart,
  name: string,
): [string, DeprecatedProperty][] {
  return Object.entries(deprecationOf(schema, part, name)?.properties ?? {}).sort(([a], [b]) => a.localeCompare(b));
}

function cleaned(change: DeprecatedProperty): DeprecatedProperty {
  const replaced = change.replaced_by?.trim();
  const note = change.note?.trim();
  return { ...(replaced ? { replaced_by: replaced } : {}), ...(note ? { note } : {}) };
}

/**
 * `entry` with the label or type itself (`property` null) or one of its
 * properties deprecated as `change`, or no longer (`change` null). Null when
 * nothing is left deprecated: the entry is then removed.
 */
export function withDeprecation(
  entry: DeprecatedEntry | undefined,
  property: string | null,
  change: DeprecatedProperty | null,
): DeprecatedEntry | null {
  const properties: Record<string, DeprecatedProperty> = Object.fromEntries(
    Object.entries(entry?.properties ?? {}).map(([name, about]) => [name, cleaned(about)]),
  );
  let own: DeprecatedEntry | null = entry?.deprecated
    ? { deprecated: true, ...cleaned({ replaced_by: entry.replaced_by, note: entry.note }) }
    : null;
  if (property === null) own = change ? { deprecated: true, ...cleaned(change) } : null;
  else if (change) properties[property] = cleaned(change);
  else delete properties[property];
  const kept = Object.keys(properties).length ? { properties } : {};
  if (!own && !Object.keys(kept).length) return null;
  return { ...(own ?? {}), ...kept };
}

/** The properties a rename or deprecation can name on a label or type: what its rows carry and what it declares. */
export function nameableProperties(schema: TenantSchema, part: SchemaPart, name: string, carried: string[]): string[] {
  const declared = Object.keys(schema.property_schema?.[part]?.[name]?.properties ?? {});
  const deprecated = Object.keys(deprecationOf(schema, part, name)?.properties ?? {});
  return [...new Set([...carried, ...declared, ...deprecated])]
    .filter((p) => !writtenByGraphos(p))
    .sort((a, b) => a.localeCompare(b));
}

/** The renames, the newest first. */
export function renamesNewestFirst(schema: TenantSchema): SchemaRename[] {
  return [...(schema.renames ?? [])].reverse();
}

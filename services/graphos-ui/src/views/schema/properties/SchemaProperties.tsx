import type { ReactNode } from "react";
import type { TenantSchema } from "../../../api/types";
import { useAddToSchema, useEdgeProperties, usePropertyDefinitions } from "../../../data/queries";
import { ALL, entryOf, type SchemaPart } from "../../../lib/propertySchema";
import { useTenantSession } from "../../../session/SessionProvider";
import { PropertyPanel, type Applied, type PropertyPanelProps } from "./PropertyPanel";

/**
 * One label's or relationship type's property panel, wired to the tenant: its
 * entry and the definitions in effect, what it inherits, and saving through
 * a registration (maintain). The Schema list and the diagram's side panel
 * both show it.
 */
export function SchemaProperties({
  schema,
  part,
  owner,
  carried,
  compact,
  showDescription,
  actions,
}: {
  schema: TenantSchema;
  part: SchemaPart;
  owner: string;
  /** Property names the rows carry. */
  carried: string[];
} & Pick<PropertyPanelProps, "compact" | "showDescription" | "actions">) {
  const { canMaintain } = useTenantSession();
  const add = useAddToSchema();
  const definitions = usePropertyDefinitions();
  const inheritedOf = useInheritedOf(schema, part, owner);

  return (
    <PropertyPanel
      part={part}
      owner={owner}
      entry={entryOf(schema, part, owner)}
      policy={owner === ALL ? undefined : definitions.data?.[part]?.[owner]}
      inheritedOf={inheritedOf}
      carried={carried}
      editable={canMaintain}
      saving={add.isPending}
      onSave={(entry, done) => add.mutate({ property_schema: { [part]: { [owner]: entry } } }, { onSuccess: done })}
      compact={compact}
      showDescription={showDescription}
      actions={actions}
    />
  );
}

/**
 * What a label's or type's property has before its own entry narrows it:
 * its parent label's definition, else the one `*` declares (none for `*`).
 */
export function useInheritedOf(
  schema: TenantSchema,
  part: SchemaPart,
  owner: string,
): (property: string) => Applied | undefined {
  const definitions = usePropertyDefinitions();
  return (property) => {
    if (owner === ALL) return undefined;
    const parent = part === "vertex" ? schema.label_hierarchy[owner] : undefined;
    if (parent) return definitions.data?.vertex[parent]?.properties[property];
    const star = entryOf(schema, part, ALL)?.properties?.[property];
    return star ? { source: ALL, definition: star } : undefined;
  };
}

/** A relationship type's carried property names, sampled from its edges while it is shown. */
export function EdgeProperties({ type, children }: { type: string; children: (carried: string[]) => ReactNode }) {
  const sampled = useEdgeProperties(type);
  return <>{children(sampled.data ?? [])}</>;
}

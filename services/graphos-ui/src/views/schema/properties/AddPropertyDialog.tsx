import { Select } from "@mantine/core";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { TenantSchema } from "../../../api/types";
import { useAddToSchema, useEdgeProperties, usePropertyDefinitions } from "../../../data/queries";
import { ALL, entryOf, undeclaredNames, withDefinition, type SchemaPart } from "../../../lib/propertySchema";
import { PropertyDialog } from "./PropertyDialog";
import { useInheritedOf } from "./SchemaProperties";

/**
 * Add > Property on the Schema page: the property dialog with, first, the
 * label or relationship type (or every one) the definition goes to. Nothing
 * is chosen for the user, so a definition never lands on every label by
 * accident. Names the chosen label's rows carry undeclared are suggested.
 */
export function AddPropertyDialog({
  schema,
  carriedByLabel,
  onClose,
}: {
  schema: TenantSchema;
  /** Property names per vertex label, from the catalogue. */
  carriedByLabel: Map<string, string[]>;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const add = useAddToSchema();
  const definitions = usePropertyDefinitions();
  const [target, setTarget] = useState<string | null>(null);
  const [part, owner] = (target?.split(":", 2) ?? ["vertex", ""]) as [SchemaPart, string];
  const inheritedOf = useInheritedOf(schema, part, owner);
  const sampled = useEdgeProperties(target && part === "edge" && owner !== ALL ? owner : null);
  const entry = owner ? entryOf(schema, part, owner) : undefined;
  const carried =
    owner === ALL || !owner ? [] : part === "edge" ? (sampled.data ?? []) : (carriedByLabel.get(owner) ?? []);

  const picker = (
    <Select
      label={t("views.schema.properties.dialog.target")}
      placeholder={t("views.schema.properties.dialog.targetPlaceholder")}
      data={[
        {
          group: t("views.schema.sections.labels"),
          items: [
            { value: `vertex:${ALL}`, label: t("views.schema.properties.allLabels") },
            ...schema.vertex_labels.map((label) => ({ value: `vertex:${label}`, label })),
          ],
        },
        {
          group: t("views.schema.sections.relationships"),
          items: [
            { value: `edge:${ALL}`, label: t("views.schema.properties.allTypes") },
            ...schema.edge_labels.map((type) => ({ value: `edge:${type}`, label: type })),
          ],
        },
      ]}
      value={target}
      onChange={setTarget}
      searchable
      allowDeselect={false}
      comboboxProps={{ withinPortal: false }}
      data-autofocus
    />
  );

  return (
    <PropertyDialog
      part={part}
      owner={owner}
      ownerPicker={picker}
      blocked={!target}
      inheritedOf={inheritedOf}
      taken={Object.keys(entry?.properties ?? {})}
      suggestions={undeclaredNames(carried, definitions.data?.[part]?.[owner])}
      saving={add.isPending}
      onCancel={onClose}
      onSave={(name, spec) =>
        add.mutate(
          { property_schema: { [part]: { [owner]: withDefinition(entry, name, spec) } } },
          { onSuccess: onClose },
        )
      }
    />
  );
}

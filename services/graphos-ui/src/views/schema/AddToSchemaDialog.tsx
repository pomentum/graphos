import { Alert, Autocomplete, Button, Group, Modal, MultiSelect, Select, Stack, Text, TextInput } from "@mantine/core";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { SchemaAddition, TenantSchema } from "../../api/types";
import { LABEL_NAME, MAX_INDEX_PROPERTIES, SEARCH_INDEX_NAME } from "./schemaModel";

export type AdditionKind = "label" | "relationship" | "index";

export interface AddToSchemaDialogProps {
  kind: AdditionKind;
  schema: TenantSchema;
  /** Property names the tenant's nodes carry, for a search index. */
  properties: string[];
  saving: boolean;
  /**
   * A relationship drawn on the diagram from one label to another: a type
   * already in the schema may be picked, and the dialog says when the line
   * will show.
   */
  pair?: { src: string; dst: string };
  onCancel: () => void;
  onAdd: (addition: SchemaAddition) => void;
}

/**
 * Asks for one addition to the schema: a label (optionally under a parent),
 * a relationship type, or a search index (name, analyzer, properties,
 * labels). Names are checked as graphos checks them, and against what the
 * schema has already, before anything is sent.
 */
export function AddToSchemaDialog({ kind, schema, properties, saving, pair, onCancel, onAdd }: AddToSchemaDialogProps) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [parent, setParent] = useState<string | null>(null);
  const [analyzer, setAnalyzer] = useState<"identifier" | "text">("identifier");
  const [language, setLanguage] = useState("english");
  const [indexProperties, setIndexProperties] = useState<string[]>(["name"]);
  const [indexLabels, setIndexLabels] = useState<string[]>([]);

  const trimmed = name.trim();
  const pattern = kind === "index" ? SEARCH_INDEX_NAME : LABEL_NAME;
  const taken =
    kind === "label"
      ? schema.vertex_labels.includes(trimmed)
      : kind === "relationship"
        ? schema.edge_labels.includes(trimmed)
        : trimmed in schema.search_indexes;
  const nameError = !trimmed
    ? null
    : !pattern.test(trimmed)
      ? t("views.schema.dialog.invalidName")
      : taken && !pair
        ? t("views.schema.dialog.exists")
        : null;
  const valid = Boolean(trimmed) && !nameError && (kind !== "index" || indexProperties.length > 0);

  const submit = () => {
    if (!valid) return;
    if (kind === "label") {
      onAdd(
        parent
          ? { vertex_labels: [trimmed, parent], label_hierarchy: { [trimmed]: parent } }
          : { vertex_labels: [trimmed] },
      );
    } else if (kind === "relationship" && pair) {
      // Declares the drawn pair for the type (registering the type when it is new).
      onAdd({
        ...(taken ? {} : { edge_labels: [trimmed] }),
        edge_endpoints: { [trimmed]: [[pair.src, pair.dst]] },
      });
    } else if (kind === "relationship") {
      onAdd({ edge_labels: [trimmed] });
    } else {
      onAdd({
        search_indexes: {
          [trimmed]: {
            labels: indexLabels.length ? indexLabels : ["*"],
            properties: indexProperties,
            analyzer,
            language: analyzer === "text" ? language : "simple",
          },
        },
      });
    }
  };

  const title = {
    label: t("views.schema.dialog.labelTitle"),
    relationship: pair
      ? t("views.schema.dialog.relationshipBetweenTitle", { src: pair.src, dst: pair.dst })
      : t("views.schema.dialog.relationshipTitle"),
    index: t("views.schema.dialog.indexTitle"),
  }[kind];
  const hint = {
    label: t("views.schema.dialog.labelNameHint"),
    relationship: t("views.schema.dialog.relationshipNameHint"),
    index: t("views.schema.dialog.indexNameHint"),
  }[kind];

  return (
    <Modal opened onClose={onCancel} title={title} centered radius="lg" size="sm">
      <Stack gap="sm">
        {pair ? (
          <Autocomplete
            label={t("views.schema.dialog.name")}
            description={t("views.schema.dialog.pairNameHint")}
            data={schema.edge_labels}
            value={name}
            error={nameError}
            data-autofocus
            onChange={setName}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />
        ) : (
          <TextInput
            label={t("views.schema.dialog.name")}
            description={hint}
            value={name}
            error={nameError}
            data-autofocus
            onChange={(e) => setName(e.currentTarget.value)}
            onKeyDown={(e) => e.key === "Enter" && kind !== "index" && submit()}
          />
        )}
        {pair ? (
          <Alert variant="light" color="gray" radius="md">
            <Text size="xs">{t("views.schema.dialog.pairNote", { src: pair.src, dst: pair.dst })}</Text>
          </Alert>
        ) : null}
        {kind === "label" ? (
          <Select
            label={t("views.schema.dialog.parent")}
            description={t("views.schema.dialog.parentHint")}
            data={schema.vertex_labels}
            value={parent}
            onChange={setParent}
            searchable
            clearable
          />
        ) : null}
        {kind === "index" ? (
          <>
            <Select
              label={t("views.schema.dialog.analyzer")}
              data={[
                { value: "identifier", label: t("views.schema.dialog.analyzerIdentifier") },
                { value: "text", label: t("views.schema.dialog.analyzerText") },
              ]}
              value={analyzer}
              onChange={(next) => setAnalyzer((next as "identifier" | "text" | null) ?? "identifier")}
              allowDeselect={false}
            />
            {analyzer === "text" ? (
              <TextInput
                label={t("views.schema.dialog.language")}
                value={language}
                onChange={(e) => setLanguage(e.currentTarget.value)}
              />
            ) : null}
            <MultiSelect
              label={t("views.schema.dialog.properties")}
              description={t("views.schema.dialog.propertiesHint", { max: MAX_INDEX_PROPERTIES })}
              data={properties}
              value={indexProperties}
              onChange={setIndexProperties}
              maxValues={MAX_INDEX_PROPERTIES}
              searchable
            />
            <MultiSelect
              label={t("views.schema.dialog.labels")}
              description={t("views.schema.dialog.labelsHint")}
              data={schema.vertex_labels}
              value={indexLabels}
              onChange={setIndexLabels}
              searchable
              clearable
            />
            <Alert variant="light" color="gray" radius="md">
              <Text size="xs">{t("views.schema.dialog.buildNote")}</Text>
            </Alert>
          </>
        ) : null}
        <Group justify="flex-end" gap="xs" mt="xs">
          <Button variant="default" size="xs" onClick={onCancel}>
            {t("common.actions.cancel")}
          </Button>
          <Button size="xs" disabled={!valid} loading={saving} onClick={submit}>
            {pair ? t("views.schema.dialog.declare") : t("views.schema.dialog.add")}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

import {
  Alert,
  Autocomplete,
  Button,
  Group,
  Modal,
  NumberInput,
  SegmentedControl,
  Select,
  Stack,
  TagsInput,
  Text,
  Textarea,
  TextInput,
} from "@mantine/core";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { ItemType, PropertySpec, PropertyType } from "../../../api/types";
import {
  enumValues,
  PROPERTY_NAME,
  rulesOf,
  sourceName,
  writtenByGraphos,
  type SchemaPart,
} from "../../../lib/propertySchema";
import type { Applied } from "./PropertyPanel";

const TYPES: PropertyType[] = ["any", "string", "number", "integer", "boolean", "array", "object"];
const ITEM_TYPES: ItemType[] = ["any", "string", "number", "integer", "boolean"];

type Presence = "optional" | "recommended" | "required";

export interface PropertyDialogProps {
  part: SchemaPart;
  /** The label or type (or `*`) whose entry the definition goes in. */
  owner: string;
  /** The property, when one is edited or narrowed; otherwise a name is asked for. */
  name?: string;
  /** What this entry declares for it already. */
  own?: PropertySpec;
  /** What a property has from `*` or an ancestor, which this entry may narrow but not retype. */
  inheritedOf: (name: string) => Applied | undefined;
  /** Names already declared in this entry (a new one may not take them). */
  taken: string[];
  /** Names the rows carry, offered for a new definition. */
  suggestions: string[];
  /** Shown first: where the definition goes, when the caller lets one choose. */
  ownerPicker?: ReactNode;
  /** Not ready to save yet (no label or type chosen). */
  blocked?: boolean;
  saving: boolean;
  onCancel: () => void;
  onSave: (name: string, spec: PropertySpec) => void;
}

/**
 * Declares one property of a label or relationship type: its JSON type,
 * whether it is required or recommended, and the values it may take (a list,
 * a pattern, a date format, numeric bounds, what an array's items are). Only
 * the keywords that fit the type are asked for. A property inherited from
 * `*` or a parent label keeps its type here; the entry adds rules to it.
 */
export function PropertyDialog({
  part,
  owner,
  name: fixedName,
  own,
  inheritedOf,
  taken,
  suggestions,
  ownerPicker,
  blocked = false,
  saving,
  onCancel,
  onSave,
}: PropertyDialogProps) {
  const { t } = useTranslation();
  const [name, setName] = useState(fixedName ?? "");
  const trimmed = name.trim();
  // What the name inherits is known as it is typed: an inherited type is kept.
  const inherited = trimmed ? inheritedOf(trimmed) : undefined;
  const inheritedType =
    inherited?.definition.type && inherited.definition.type !== "any" ? inherited.definition.type : null;
  const [chosenType, setType] = useState<PropertyType>(own?.type ?? "any");
  const type = inheritedType ?? chosenType;
  const [presence, setPresence] = useState<Presence>(
    own?.required ? "required" : own?.recommended ? "recommended" : "optional",
  );
  const [enumText, setEnumText] = useState<string[]>((own?.enum ?? []).map(String));
  const [pattern, setPattern] = useState(own?.pattern ?? "");
  const [format, setFormat] = useState<string | null>(own?.format ?? null);
  const [minimum, setMinimum] = useState<number | string>(own?.minimum ?? "");
  const [maximum, setMaximum] = useState<number | string>(own?.maximum ?? "");
  const [itemType, setItemType] = useState<ItemType>(own?.items?.type ?? "any");
  const [itemEnum, setItemEnum] = useState<string[]>((own?.items?.enum ?? []).map(String));
  const [itemPattern, setItemPattern] = useState(own?.items?.pattern ?? "");
  const [description, setDescription] = useState(own?.description ?? "");

  // Untyped, only the allowed values are asked for, unless a keyword is set already.
  const untyped = type === "any";
  const text = type === "string" || (untyped && Boolean(own?.pattern || own?.format));
  const numeric = type === "number" || type === "integer" || (untyped && (own?.minimum ?? own?.maximum) !== undefined);
  const listable = type !== "array" && type !== "object";
  const enumParsed = enumValues(enumText, type);
  const itemEnumParsed = enumValues(itemEnum, itemType);
  const nameError = !trimmed
    ? null
    : !PROPERTY_NAME.test(trimmed)
      ? t("views.schema.properties.dialog.invalidName")
      : writtenByGraphos(trimmed) && trimmed !== "id"
        ? t("views.schema.properties.dialog.reserved")
        : !fixedName && taken.includes(trimmed)
          ? t("views.schema.properties.dialog.taken")
          : null;
  const rangeError =
    typeof minimum === "number" && typeof maximum === "number" && minimum > maximum
      ? t("views.schema.properties.dialog.badRange")
      : null;
  const enumError = enumParsed === null ? t("views.schema.properties.dialog.badEnum", { type }) : null;
  const itemEnumError =
    itemEnumParsed === null ? t("views.schema.properties.dialog.badEnum", { type: itemType }) : null;
  // `id` is the node's own key: only its shape may be declared.
  const isId = trimmed === "id";
  const valid = !blocked && Boolean(trimmed) && !nameError && !rangeError && !enumError && !itemEnumError;

  const spec = (): PropertySpec => {
    if (isId) return { ...(pattern.trim() ? { pattern: pattern.trim() } : {}), ...descriptionOf(description) };
    const out: PropertySpec = {};
    // An inherited type is the property's already; only a new one is stated.
    if (type !== "any" && !inheritedType) out.type = type;
    if (presence === "required") out.required = true;
    if (presence === "recommended") out.recommended = true;
    if (listable && enumParsed?.length) out.enum = enumParsed;
    if (text && pattern.trim()) out.pattern = pattern.trim();
    if (text && format) out.format = format as PropertySpec["format"];
    if (numeric && typeof minimum === "number") out.minimum = minimum;
    if (numeric && typeof maximum === "number") out.maximum = maximum;
    if (type === "array") {
      const items = {
        ...(itemType !== "any" ? { type: itemType } : {}),
        ...(itemEnumParsed?.length ? { enum: itemEnumParsed } : {}),
        ...(itemPattern.trim() && (itemType === "string" || itemType === "any") ? { pattern: itemPattern.trim() } : {}),
      };
      if (Object.keys(items).length) out.items = items;
    }
    return { ...out, ...descriptionOf(description) };
  };
  const submit = () => valid && onSave(trimmed, spec());

  const title = fixedName
    ? t("views.schema.properties.dialog.editTitle", { name: fixedName, owner: sourceName(part, owner) })
    : ownerPicker
      ? t("views.schema.properties.dialog.addTitle")
      : t("views.schema.properties.dialog.newTitle", { owner: sourceName(part, owner) });

  return (
    <Modal opened onClose={onCancel} title={title} centered radius="lg" size="md">
      <Stack gap="sm">
        {ownerPicker}
        {fixedName ? null : (
          <Autocomplete
            label={t("views.schema.properties.dialog.name")}
            description={t("views.schema.properties.dialog.nameHint")}
            data={suggestions.filter((s) => !taken.includes(s))}
            value={name}
            error={nameError}
            onChange={setName}
            data-autofocus={ownerPicker ? undefined : true}
          />
        )}
        {inherited ? (
          <Alert variant="light" color="gray" radius="md">
            <Text size="xs">
              {t("views.schema.properties.dialog.inherits", {
                source: sourceName(part, inherited.source),
                rules: rulesOf(inherited.definition).join(", ") || t("views.schema.properties.rules.anything"),
              })}
            </Text>
          </Alert>
        ) : null}
        {isId ? (
          <Text size="xs" c="dimmed">
            {t("views.schema.properties.dialog.idOnly")}
          </Text>
        ) : (
          <>
            <Select
              label={t("views.schema.properties.dialog.type")}
              description={inheritedType ? t("views.schema.properties.dialog.typeInherited") : undefined}
              data={TYPES.map((value) => ({ value, label: t(`views.schema.properties.rules.typeNames.${value}`) }))}
              value={type}
              onChange={(next) => next && setType(next as PropertyType)}
              disabled={Boolean(inheritedType)}
              allowDeselect={false}
              comboboxProps={{ withinPortal: false }}
            />
            <div>
              <Text size="sm" fw={500} mb={4}>
                {t("views.schema.properties.dialog.presence")}
              </Text>
              <SegmentedControl
                size="xs"
                fullWidth
                value={presence}
                onChange={(next) => setPresence(next as Presence)}
                data={(["optional", "recommended", "required"] as const).map((value) => ({
                  value,
                  label: t(`views.schema.properties.dialog.presences.${value}`),
                }))}
              />
            </div>
            {listable && type !== "boolean" ? (
              <TagsInput
                label={t("views.schema.properties.dialog.enum")}
                description={t("views.schema.properties.dialog.enumHint")}
                value={enumText}
                onChange={setEnumText}
                error={enumError}
                clearable
              />
            ) : null}
            {text ? (
              <Group grow align="flex-start">
                <TextInput
                  label={t("views.schema.properties.dialog.pattern")}
                  description={t("views.schema.properties.dialog.patternHint")}
                  value={pattern}
                  onChange={(e) => setPattern(e.currentTarget.value)}
                  styles={{ input: { fontFamily: "var(--mantine-font-family-monospace)" } }}
                />
                <Select
                  label={t("views.schema.properties.dialog.format")}
                  placeholder={t("views.schema.properties.dialog.noFormat")}
                  data={[
                    { value: "date", label: t("views.schema.properties.rules.formats.date") },
                    { value: "date-time", label: t("views.schema.properties.rules.formats.date-time") },
                  ]}
                  value={format}
                  onChange={setFormat}
                  clearable
                  comboboxProps={{ withinPortal: false }}
                />
              </Group>
            ) : null}
            {numeric ? (
              <Group grow align="flex-start">
                <NumberInput
                  label={t("views.schema.properties.dialog.minimum")}
                  value={minimum}
                  onChange={setMinimum}
                  error={rangeError}
                />
                <NumberInput
                  label={t("views.schema.properties.dialog.maximum")}
                  value={maximum}
                  onChange={setMaximum}
                />
              </Group>
            ) : null}
            {type === "array" ? (
              <Stack gap={6}>
                <Text size="sm" fw={500}>
                  {t("views.schema.properties.dialog.items")}
                </Text>
                <Group grow align="flex-start">
                  <Select
                    aria-label={t("views.schema.properties.dialog.itemType")}
                    data={ITEM_TYPES.map((value) => ({
                      value,
                      label: t(`views.schema.properties.rules.typeNames.${value}`),
                    }))}
                    value={itemType}
                    onChange={(next) => next && setItemType(next as ItemType)}
                    allowDeselect={false}
                    comboboxProps={{ withinPortal: false }}
                  />
                  {itemType === "string" || itemType === "any" ? (
                    <TextInput
                      aria-label={t("views.schema.properties.dialog.itemPattern")}
                      placeholder={t("views.schema.properties.dialog.itemPattern")}
                      value={itemPattern}
                      onChange={(e) => setItemPattern(e.currentTarget.value)}
                      styles={{ input: { fontFamily: "var(--mantine-font-family-monospace)" } }}
                    />
                  ) : null}
                </Group>
                {itemType !== "boolean" ? (
                  <TagsInput
                    aria-label={t("views.schema.properties.dialog.itemEnum")}
                    placeholder={t("views.schema.properties.dialog.itemEnum")}
                    value={itemEnum}
                    onChange={setItemEnum}
                    error={itemEnumError}
                    clearable
                  />
                ) : null}
              </Stack>
            ) : null}
          </>
        )}
        <Textarea
          label={t("views.schema.properties.dialog.description")}
          value={description}
          onChange={(e) => setDescription(e.currentTarget.value)}
          autosize
          minRows={1}
          maxRows={4}
        />
        <Group justify="flex-end" gap="xs" mt="xs">
          <Button variant="default" size="xs" onClick={onCancel}>
            {t("common.actions.cancel")}
          </Button>
          <Button size="xs" disabled={!valid} loading={saving} onClick={submit}>
            {t("views.schema.properties.dialog.save")}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function descriptionOf(text: string): Pick<PropertySpec, "description"> {
  return text.trim() ? { description: text.trim() } : {};
}

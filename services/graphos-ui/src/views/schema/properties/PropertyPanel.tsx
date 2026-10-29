import {
  ActionIcon,
  Badge,
  Button,
  Group,
  Modal,
  Select,
  Stack,
  Table,
  Text,
  Textarea,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { IconPencil, IconPlus, IconSettings, IconTrash } from "@tabler/icons-react";
import { useState, type ReactElement, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { EffectivePolicy, PropertyEntry, PropertyMode, PropertySpec, UndeclaredPolicy } from "../../../api/types";
import {
  ALL,
  rulesOf,
  sourceName,
  undeclaredNames,
  withDefinition,
  withSettings,
  type SchemaPart,
} from "../../../lib/propertySchema";
import { LockedHint } from "../SchemaManager";
import { PropertyDialog } from "./PropertyDialog";
import classes from "./PropertyPanel.module.css";

/** A definition in effect, and the one it narrows from `*` or a parent. */
export interface Applied {
  source: string;
  definition: PropertySpec;
}

export interface PropertyPanelProps {
  part: SchemaPart;
  /** The label or type (or `*`). */
  owner: string;
  /** What this label or type declares itself. */
  entry: PropertyEntry | undefined;
  /** What its writes are checked against (none for `*`, which is never written as such). */
  policy: EffectivePolicy | undefined;
  /** The definition a property inherits, before this entry narrows it. */
  inheritedOf: (property: string) => Applied | undefined;
  /** Property names the rows carry. */
  carried: string[];
  editable: boolean;
  /**
   * Why an editable panel's controls are disabled (a program manages the
   * schema): they stay in view, with this as their tooltip. Null otherwise.
   */
  locked?: string | null;
  saving: boolean;
  /** The entry to declare, or `null` to remove it. */
  onSave: (entry: PropertyEntry | null, done: () => void) => void;
  /** A list instead of a table, for a narrow side panel. */
  compact?: boolean;
  /** The description line (left out where the heading shows it). */
  showDescription?: boolean;
  /** More buttons, on the row of "Define a property". */
  actions?: ReactNode;
}

type Editing = { name?: string } | null;

/**
 * The properties of one label or relationship type: each definition in
 * effect with where it comes from (this entry, `*` or a parent label), and
 * the names its rows carry that nothing declares. Maintainers declare,
 * narrow and remove definitions here, and set the entry's description, mode
 * and what happens to undeclared properties; while `locked`, those controls
 * show disabled.
 */
export function PropertyPanel({
  part,
  owner,
  entry,
  policy,
  inheritedOf,
  carried,
  editable,
  locked = null,
  saving,
  onSave,
  compact = false,
  showDescription = true,
  actions,
}: PropertyPanelProps) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState<Editing>(null);
  const [settings, setSettings] = useState(false);
  const own = entry?.properties ?? {};
  // `*` has no policy of its own: its rows are its declarations.
  const applied: Record<string, Applied> =
    owner === ALL
      ? Object.fromEntries(Object.entries(own).map(([name, definition]) => [name, { source: ALL, definition }]))
      : (policy?.properties ?? {});
  const names = Object.keys(applied).sort();
  const undeclared = owner === ALL ? [] : undeclaredNames(carried, policy);
  const save = (next: PropertyEntry | null) => onSave(next, () => setEditing(null));
  const from = (source: string) =>
    source === owner ? null : t("views.schema.properties.from", { source: sourceName(part, source) });
  const rowActions = (name: string) =>
    editable ? (
      <RowActions
        name={name}
        declaredHere={name in own}
        locked={locked}
        saving={saving}
        onEdit={() => setEditing({ name })}
        onRemove={() => save(withDefinition(entry, name, null))}
      />
    ) : null;

  return (
    <Stack gap={6}>
      <Group gap={6} wrap="nowrap" align="flex-start">
        <Text size="xs" c={entry?.description ? undefined : "dimmed"} className={classes.description}>
          {showDescription ? (entry?.description ?? (editable ? t("views.schema.properties.noDescription") : "")) : ""}
        </Text>
        {/* The mode is the tenant's unless the entry sets its own. */}
        {entry?.mode ? (
          <Tooltip label={t("views.schema.properties.modeTooltip")} withArrow>
            <Badge size="xs" variant="light" radius="sm" tt="none" color={MODE_COLOR[entry.mode]}>
              {t(`views.schema.properties.modes.${entry.mode}`)}
            </Badge>
          </Tooltip>
        ) : null}
        {policy && policy.undeclared !== "allow" ? (
          <Badge size="xs" variant="light" radius="sm" tt="none" color="gray">
            {t(`views.schema.properties.undeclaredIs.${policy.undeclared}`)}
          </Badge>
        ) : null}
        {editable ? (
          <Hint label={t("views.schema.properties.settings")} locked={locked}>
            <ActionIcon
              size="sm"
              variant="subtle"
              aria-label={t("views.schema.properties.settingsOf", { owner: sourceName(part, owner) })}
              disabled={locked !== null}
              onClick={() => setSettings(true)}
            >
              <IconSettings size={14} />
            </ActionIcon>
          </Hint>
        ) : null}
      </Group>

      {names.length === 0 ? (
        <Text size="xs" c="dimmed">
          {t("views.schema.properties.none")}
        </Text>
      ) : compact ? (
        <div className={classes.list}>
          {names.map((name) => (
            <div key={name} className={classes.item}>
              <Group gap={6} wrap="nowrap">
                <Text size="xs" className={classes.itemName}>
                  {name}
                </Text>
                <Text size="xs" c="dimmed" className={classes.itemSource} truncate>
                  {from(applied[name]!.source)}
                </Text>
                {rowActions(name)}
              </Group>
              <Rules definition={applied[name]!.definition} />
            </div>
          ))}
        </div>
      ) : (
        <Table className={classes.table} verticalSpacing={4} horizontalSpacing={8} fz="xs">
          <Table.Tbody>
            {names.map((name) => (
              <Table.Tr key={name}>
                <Table.Td className={classes.name}>{name}</Table.Td>
                <Table.Td>
                  <Rules definition={applied[name]!.definition} />
                </Table.Td>
                <Table.Td className={classes.source}>{from(applied[name]!.source)}</Table.Td>
                {editable ? <Table.Td className={classes.actions}>{rowActions(name)}</Table.Td> : null}
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}

      {undeclared.length ? (
        <Group gap={4} align="center">
          <Text size="xs" c="dimmed">
            {t("views.schema.properties.carried")}
          </Text>
          {undeclared.map((name) =>
            editable && locked === null ? (
              <Tooltip key={name} label={t("views.schema.properties.declare", { name })} withArrow>
                <UnstyledButton
                  onClick={() => setEditing({ name })}
                  aria-label={t("views.schema.properties.declare", { name })}
                >
                  <Badge size="sm" variant="outline" radius="sm" tt="none" color="gray" className={classes.carried}>
                    {name}
                  </Badge>
                </UnstyledButton>
              </Tooltip>
            ) : (
              <Badge key={name} size="sm" variant="outline" radius="sm" tt="none" color="gray">
                {name}
              </Badge>
            ),
          )}
        </Group>
      ) : null}

      {editable || actions ? (
        <Group gap="xs">
          {editable ? (
            <LockedHint reason={locked}>
              <Button
                size="compact-xs"
                variant="subtle"
                leftSection={<IconPlus size={12} />}
                disabled={locked !== null}
                onClick={() => setEditing({})}
              >
                {t("views.schema.properties.add")}
              </Button>
            </LockedHint>
          ) : null}
          {actions}
        </Group>
      ) : null}

      {editing ? (
        <PropertyDialog
          part={part}
          owner={owner}
          name={editing.name}
          own={editing.name ? own[editing.name] : undefined}
          inheritedOf={inheritedOf}
          taken={Object.keys(own)}
          suggestions={undeclared}
          saving={saving}
          onCancel={() => setEditing(null)}
          onSave={(name, spec) => save(withDefinition(entry, name, spec))}
        />
      ) : null}
      {settings ? (
        <EntrySettings
          part={part}
          owner={owner}
          entry={entry}
          saving={saving}
          onCancel={() => setSettings(false)}
          onSave={(next) => onSave(next, () => setSettings(false))}
        />
      ) : null}
    </Stack>
  );
}

const MODE_COLOR: Record<PropertyMode, string> = { off: "gray", warn: "yellow", strict: "red" };

/** Chip text longer than this may be cut by the chip's width (`.rule`). */
const CLIPPED_AT = 40;

/** A definition's rules as chips; one shortened or cut says all of itself on hover. */
function Rules({ definition }: { definition: PropertySpec }) {
  const { t } = useTranslation();
  const short = rulesOf(definition);
  const full = rulesOf(definition, true);
  return (
    <Group gap={4}>
      {short.length ? (
        short.map((rule, i) => (
          <Tooltip
            key={rule}
            label={full[i]}
            disabled={full[i] === rule && rule.length <= CLIPPED_AT}
            withArrow
            multiline
            maw={360}
          >
            <Badge size="xs" variant="default" radius="sm" tt="none" className={classes.rule}>
              {rule}
            </Badge>
          </Tooltip>
        ))
      ) : (
        <Badge size="xs" variant="default" radius="sm" tt="none" className={classes.rule}>
          {t("views.schema.properties.rules.anything")}
        </Badge>
      )}
    </Group>
  );
}

/** A control's tooltip: what it does, or, while `locked`, why it is disabled. */
function Hint({ label, locked, children }: { label: string; locked: string | null; children: ReactElement }) {
  return locked ? (
    <LockedHint reason={locked}>{children}</LockedHint>
  ) : (
    <Tooltip label={label} withArrow>
      {children}
    </Tooltip>
  );
}

/** Edit (or narrow, when inherited) and remove (when declared here) one definition. */
function RowActions({
  name,
  declaredHere,
  locked,
  saving,
  onEdit,
  onRemove,
}: {
  name: string;
  declaredHere: boolean;
  locked: string | null;
  saving: boolean;
  onEdit: () => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Group gap={2} wrap="nowrap" justify="flex-end" className={classes.rowActions}>
      <Hint
        label={declaredHere ? t("views.schema.properties.edit") : t("views.schema.properties.narrow")}
        locked={locked}
      >
        <ActionIcon
          size="sm"
          variant="subtle"
          aria-label={t("views.schema.properties.editOf", { name })}
          disabled={locked !== null}
          onClick={onEdit}
        >
          <IconPencil size={14} />
        </ActionIcon>
      </Hint>
      {declaredHere ? (
        <Hint label={t("views.schema.properties.remove")} locked={locked}>
          <ActionIcon
            size="sm"
            variant="subtle"
            color="red"
            aria-label={t("views.schema.properties.removeOf", { name })}
            loading={saving}
            disabled={locked !== null}
            onClick={onRemove}
          >
            <IconTrash size={14} />
          </ActionIcon>
        </Hint>
      ) : null}
    </Group>
  );
}

/** An entry's description, its own mode and its rule for undeclared properties. */
function EntrySettings({
  part,
  owner,
  entry,
  saving,
  onCancel,
  onSave,
}: {
  part: SchemaPart;
  owner: string;
  entry: PropertyEntry | undefined;
  saving: boolean;
  onCancel: () => void;
  onSave: (entry: PropertyEntry | null) => void;
}) {
  const { t } = useTranslation();
  const [description, setDescription] = useState(entry?.description ?? "");
  const [mode, setMode] = useState<string>(entry?.mode ?? "");
  const [undeclared, setUndeclared] = useState<string>(entry?.undeclared ?? "");
  const inherit = { value: "", label: t("views.schema.properties.inherit") };
  return (
    <Modal
      opened
      onClose={onCancel}
      title={t("views.schema.properties.settingsTitle", { owner: sourceName(part, owner) })}
      centered
      radius="lg"
      size="sm"
    >
      <Stack gap="sm">
        {owner === ALL ? null : (
          <Textarea
            label={t("views.schema.properties.description")}
            description={t("views.schema.properties.descriptionHint")}
            value={description}
            onChange={(e) => setDescription(e.currentTarget.value)}
            autosize
            minRows={2}
            maxRows={5}
            data-autofocus
          />
        )}
        <Select
          label={t("views.schema.properties.ownMode")}
          description={t("views.schema.properties.ownModeHint")}
          data={[
            inherit,
            ...(["off", "warn", "strict"] as const).map((value) => ({
              value,
              label: t(`views.schema.properties.modes.${value}`),
            })),
          ]}
          value={mode}
          onChange={(next) => setMode(next ?? "")}
          allowDeselect={false}
          comboboxProps={{ withinPortal: false }}
        />
        <Select
          label={t("views.schema.properties.undeclared")}
          description={t("views.schema.properties.undeclaredHint")}
          data={[
            inherit,
            ...(["allow", "warn", "reject"] as const).map((value) => ({
              value,
              label: t(`views.schema.properties.undeclaredPolicies.${value}`),
            })),
          ]}
          value={undeclared}
          onChange={(next) => setUndeclared(next ?? "")}
          allowDeselect={false}
          comboboxProps={{ withinPortal: false }}
        />
        <Group justify="flex-end" gap="xs" mt="xs">
          <Button variant="default" size="xs" onClick={onCancel}>
            {t("common.actions.cancel")}
          </Button>
          <Button
            size="xs"
            loading={saving}
            onClick={() =>
              onSave(
                withSettings(entry, {
                  description,
                  mode: (mode || undefined) as PropertyMode | undefined,
                  undeclared: (undeclared || undefined) as UndeclaredPolicy | undefined,
                }),
              )
            }
          >
            {t("views.schema.properties.dialog.save")}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

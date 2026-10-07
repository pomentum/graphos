import { Alert, Button, Group, Skeleton, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { Versioned, VersionedEditor } from "../data/useVersionedEditor";
import { PropertyEditor } from "./PropertyEditor";

export interface RecordFormProps<T extends Versioned> {
  editor: VersionedEditor<T>;
  editable: boolean;
  /** Property names offered when adding one. */
  suggestions?: string[];
  /** Called with the record as saved. */
  onSaved?: (record: T) => void;
  /** Shown at the start of the action row (e.g. a Delete button). */
  actions?: ReactNode;
}

/**
 * The editable properties of a versioned record (node or edge): the form,
 * Revert and Save while there are changes, and the notice when someone else
 * saved meanwhile. The rules live in `useVersionedEditor`.
 */
export function RecordForm<T extends Versioned>({
  editor,
  editable,
  suggestions,
  onSaved,
  actions,
}: RecordFormProps<T>) {
  const { t } = useTranslation();
  const save = async () => {
    const saved = await editor.save();
    if (saved) onSaved?.(saved);
  };

  return (
    <Stack gap="md">
      {editor.stale ? (
        <Alert
          color="orange"
          variant="light"
          radius="md"
          title={t("components.recordForm.changedBy", {
            who: editor.changedBy ?? t("components.recordForm.someone"),
          })}
        >
          <Text size="xs">{t("components.recordForm.editsKept")}</Text>
          <Group gap="xs" mt="xs">
            <Button size="compact-xs" variant="default" onClick={editor.discard}>
              {t("components.recordForm.discardMine")}
            </Button>
            <Button size="compact-xs" loading={editor.saving} onClick={() => void save()}>
              {t("components.recordForm.saveMine")}
            </Button>
          </Group>
        </Alert>
      ) : null}

      {editor.isLoading ? (
        <Skeleton h={120} radius="lg" />
      ) : (
        <PropertyEditor draft={editor.values} editable={editable} onChange={editor.change} suggestions={suggestions} />
      )}
      {editor.error ? (
        <Text size="xs" c="red">
          {editor.error}
        </Text>
      ) : null}

      {editable ? (
        <Group justify="space-between">
          {actions ?? <span />}
          {editor.dirty && !editor.stale ? (
            <Group gap="xs">
              <Button size="xs" variant="default" onClick={editor.discard}>
                {t("common.actions.revert")}
              </Button>
              <Button size="xs" loading={editor.saving} onClick={() => void save()}>
                {t("common.actions.save")}
              </Button>
            </Group>
          ) : null}
        </Group>
      ) : null}
    </Stack>
  );
}

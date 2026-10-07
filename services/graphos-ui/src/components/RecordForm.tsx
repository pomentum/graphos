import { Alert, Button, Group, Skeleton, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";
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
  const save = async () => {
    const saved = await editor.save();
    if (saved) onSaved?.(saved);
  };

  return (
    <Stack gap="md">
      {editor.stale ? (
        <Alert color="orange" variant="light" radius="md" title={`${editor.changedBy ?? "Someone"} changed this`}>
          <Text size="xs">Your edits are kept. Saving applies only the fields you changed.</Text>
          <Group gap="xs" mt="xs">
            <Button size="compact-xs" variant="default" onClick={editor.discard}>
              Discard mine
            </Button>
            <Button size="compact-xs" loading={editor.saving} onClick={() => void save()}>
              Save mine
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
                Revert
              </Button>
              <Button size="xs" loading={editor.saving} onClick={() => void save()}>
                Save
              </Button>
            </Group>
          ) : null}
        </Group>
      ) : null}
    </Stack>
  );
}

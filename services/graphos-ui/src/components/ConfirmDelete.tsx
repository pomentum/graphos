import { Button, Popover, Text } from "@mantine/core";
import { IconTrash } from "@tabler/icons-react";

/** A quiet Delete button that asks once before calling `onConfirm`. */
export function ConfirmDelete({
  question,
  confirmLabel,
  busy,
  onConfirm,
}: {
  question: string;
  confirmLabel: string;
  busy: boolean;
  onConfirm: () => void;
}) {
  return (
    <Popover position="bottom-start" withArrow>
      <Popover.Target>
        <Button size="xs" variant="subtle" color="red" leftSection={<IconTrash size={14} />}>
          Delete
        </Button>
      </Popover.Target>
      <Popover.Dropdown>
        <Text size="xs" mb="xs">
          {question}
        </Text>
        <Button size="compact-xs" color="red" loading={busy} onClick={onConfirm}>
          {confirmLabel}
        </Button>
      </Popover.Dropdown>
    </Popover>
  );
}

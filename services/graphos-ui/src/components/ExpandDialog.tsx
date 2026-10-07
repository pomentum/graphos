import { Button, Checkbox, Group, Modal, Stack, Text } from "@mantine/core";
import { useEffect, useState } from "react";
import type { EdgeTypeCount } from "../lib/expand";
import { formatCount } from "../lib/time";

export interface ExpandDialogProps {
  opened: boolean;
  nodeName: string;
  counts: EdgeTypeCount[];
  /** graphos returned only the first part of the neighbours. */
  truncated: boolean;
  onCancel: () => void;
  onConfirm: (edgeTypes: string[]) => void;
}

/**
 * Asks which kinds of neighbours to add when a node has many: one checkbox
 * per edge type with its count, so a hub never floods the canvas.
 */
export function ExpandDialog({ opened, nodeName, counts, truncated, onCancel, onConfirm }: ExpandDialogProps) {
  const [chosen, setChosen] = useState<string[]>([]);
  useEffect(() => setChosen([]), [counts]);
  const total = counts.filter((c) => chosen.includes(c.type)).reduce((sum, c) => sum + c.in + c.out, 0);

  return (
    <Modal opened={opened} onClose={onCancel} title={`${nodeName} has many neighbours`} centered radius="lg" size="sm">
      <Text size="sm" c="dimmed" mb="md">
        Choose which connections to add{truncated ? ". Counts cover the first neighbours only." : "."}
      </Text>
      <Checkbox.Group value={chosen} onChange={setChosen}>
        <Stack gap="xs">
          {counts.map((c) => (
            <Checkbox
              key={c.type}
              value={c.type}
              label={
                <Group gap={6}>
                  <Text size="sm">{c.type}</Text>
                  <Text size="xs" c="dimmed">
                    {formatCount(c.out + c.in)}
                    {truncated ? "+" : ""}
                  </Text>
                </Group>
              }
            />
          ))}
        </Stack>
      </Checkbox.Group>
      <Group justify="flex-end" mt="lg" gap="xs">
        <Button variant="default" size="xs" onClick={onCancel}>
          Cancel
        </Button>
        <Button size="xs" disabled={chosen.length === 0} onClick={() => onConfirm(chosen)}>
          {chosen.length ? `Add ${formatCount(total)}${truncated ? "+" : ""}` : "Add"}
        </Button>
      </Group>
    </Modal>
  );
}

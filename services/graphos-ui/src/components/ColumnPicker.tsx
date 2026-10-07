import { Button, Checkbox, Popover, ScrollArea, Stack } from "@mantine/core";
import { IconColumns3 } from "@tabler/icons-react";

export interface ColumnPickerProps {
  /** Every column that can be shown, in the order offered. */
  options: string[];
  /** The columns shown, in display order. */
  value: string[];
  onChange: (columns: string[]) => void;
}

/** A Columns button with a checklist; checked columns keep the order of `options`. */
export function ColumnPicker({ options, value, onChange }: ColumnPickerProps) {
  const shown = new Set(value);
  const toggle = (column: string, on: boolean) => {
    const next = new Set(shown);
    if (on) next.add(column);
    else next.delete(column);
    onChange(options.filter((option) => next.has(option)));
  };

  return (
    <Popover position="bottom-end" shadow="md" width={220}>
      <Popover.Target>
        <Button size="xs" variant="default" leftSection={<IconColumns3 size={14} />}>
          Columns · {value.length}
        </Button>
      </Popover.Target>
      <Popover.Dropdown p="xs">
        <ScrollArea.Autosize mah={320}>
          <Stack gap={8} p={4}>
            {options.map((column) => (
              <Checkbox
                key={column}
                size="xs"
                label={column}
                checked={shown.has(column)}
                onChange={(event) => toggle(column, event.currentTarget.checked)}
              />
            ))}
          </Stack>
        </ScrollArea.Autosize>
      </Popover.Dropdown>
    </Popover>
  );
}

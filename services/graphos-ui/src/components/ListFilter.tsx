import { CloseButton, TextInput } from "@mantine/core";
import { IconSearch } from "@tabler/icons-react";

/** A list gets a filter once it is longer than this. */
export const FILTER_FROM = 8;

/** Whether `filter` (any case) is in one of `texts`; an empty filter matches all. */
export function matches(filter: string, ...texts: (string | null | undefined)[]): boolean {
  const wanted = filter.trim().toLowerCase();
  return !wanted || texts.some((text) => text?.toLowerCase().includes(wanted));
}

/** A small field that filters a list as you type. */
export function ListFilter({
  value,
  onChange,
  label,
  w = 220,
}: {
  value: string;
  onChange: (value: string) => void;
  label: string;
  w?: number | string;
}) {
  return (
    <TextInput
      size="xs"
      w={w}
      value={value}
      onChange={(event) => onChange(event.currentTarget.value)}
      placeholder={label}
      aria-label={label}
      leftSection={<IconSearch size={14} />}
      rightSection={value ? <CloseButton size="xs" aria-label={label} onClick={() => onChange("")} /> : null}
    />
  );
}

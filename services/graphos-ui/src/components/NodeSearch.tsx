import { Combobox, Group, Kbd, Loader, Text, TextInput, useCombobox } from "@mantine/core";
import { useDebouncedValue, useHotkeys } from "@mantine/hooks";
import { IconSearch } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import type { SearchHit } from "../api/types";
import { useSchema, useSearch } from "../data/queries";
import { pickSearchIndex } from "../data/searchIndex";
import { displayName } from "../graph/GraphModel";

export type PickMode = "open" | "add";

export interface NodeSearchProps {
  /** Called with the chosen hit; `add` when Shift was held (Shift Return / Shift click). */
  onPick: (hit: SearchHit, mode: PickMode) => void;
  placeholder?: string;
  /** Focus with Ctrl K / ⌘K. Only one search on screen should claim it. */
  hotkey?: boolean;
  /** Show the "Shift Return adds" hint. */
  allowAdd?: boolean;
  /** Keep the chosen node's name in the field (for pickers) instead of clearing it. */
  keepSelection?: boolean;
  width?: number | string;
}

/**
 * A search field with a dropdown of ranked hits from the tenant's search
 * index. Used for the toolbar search and as a node picker in forms.
 */
export function NodeSearch({
  onPick,
  placeholder = "Search",
  hotkey,
  allowAdd,
  keepSelection,
  width = 260,
}: NodeSearchProps) {
  const [query, setQuery] = useState("");
  const [debounced] = useDebouncedValue(query, 150);
  const shift = useRef(false);
  const input = useRef<HTMLInputElement>(null);
  const combobox = useCombobox({ onDropdownClose: () => combobox.resetSelectedOption() });
  const schema = useSchema();
  const index = pickSearchIndex(schema.data);
  const search = useSearch({ q: debounced, index, limit: 8 });
  const hits = debounced.trim() ? (search.data?.nodes ?? []) : [];

  useHotkeys(hotkey ? [["mod+K", () => input.current?.focus()]] : []);

  // The top hit is highlighted, so Return opens it without arrowing down.
  const firstHit = hits[0]?.id;
  const store = useRef(combobox);
  store.current = combobox;
  useEffect(() => {
    if (firstHit !== undefined) store.current.selectFirstOption();
  }, [firstHit]);

  const pick = (value: string) => {
    const hit = hits.find((h) => String(h.id) === value);
    if (!hit) return;
    onPick(hit, shift.current ? "add" : "open");
    setQuery(keepSelection ? displayName(hit.properties, String(hit.properties.id ?? hit.id)) : "");
    combobox.closeDropdown();
    if (!keepSelection) input.current?.blur();
  };

  return (
    <Combobox store={combobox} onOptionSubmit={pick} width={Math.max(Number(width) || 0, 280)} position="bottom-end">
      <Combobox.Target>
        <TextInput
          ref={input}
          value={query}
          w={width}
          size="xs"
          radius="md"
          placeholder={placeholder}
          aria-label={placeholder}
          leftSection={<IconSearch size={14} />}
          rightSection={search.isFetching ? <Loader size={12} /> : hotkey ? <Kbd size="xs">Ctrl K</Kbd> : null}
          rightSectionWidth={hotkey ? 56 : 28}
          onChange={(event) => {
            setQuery(event.currentTarget.value);
            combobox.openDropdown();
            combobox.updateSelectedOptionIndex();
          }}
          onFocus={() => query && combobox.openDropdown()}
          onBlur={() => combobox.closeDropdown()}
          onKeyDown={(event) => {
            shift.current = event.shiftKey;
            if (event.key === "Escape") setQuery("");
          }}
        />
      </Combobox.Target>
      <Combobox.Dropdown hidden={!debounced.trim()}>
        <Combobox.Options>
          {hits.length === 0 && !search.isFetching ? <Combobox.Empty>No matches</Combobox.Empty> : null}
          {hits.map((hit) => (
            <Combobox.Option
              key={hit.id}
              value={String(hit.id)}
              onMouseDown={(event) => {
                shift.current = event.shiftKey;
              }}
            >
              <Group justify="space-between" wrap="nowrap" gap="sm">
                <Text size="sm" truncate>
                  {displayName(hit.properties, String(hit.properties.id ?? hit.id))}
                </Text>
                <Text size="xs" c="dimmed">
                  {hit.label}
                </Text>
              </Group>
            </Combobox.Option>
          ))}
        </Combobox.Options>
        {allowAdd && hits.length > 0 ? (
          <Combobox.Footer>
            <Text size="xs" c="dimmed">
              Return opens · Shift Return adds to the canvas
            </Text>
          </Combobox.Footer>
        ) : null}
      </Combobox.Dropdown>
    </Combobox>
  );
}

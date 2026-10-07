import { Combobox, Group, Kbd, Loader, ScrollArea, Select, Stack, Text, TextInput, useCombobox } from "@mantine/core";
import { useDebouncedValue, useHotkeys, type HotkeyItem } from "@mantine/hooks";
import { IconSearch } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import type { SearchHit } from "../api/types";
import { usePropertyNames, useSchema, useSearch } from "../data/queries";
import { pickSearchIndex } from "../data/searchIndex";
import { displayName } from "../graph/GraphModel";
import { isApple, searchShortcut } from "../lib/platform";
import { middleEllipsis } from "../lib/text";
import { useTenantSession } from "../session/SessionProvider";

export type PickMode = "open" | "add";

/** Hits fetched per query; the list scrolls. */
const HIT_LIMIT = 20;
/** Characters of an id shown under a hit's name; the rest is cut from the middle. */
const ID_CHARS = 48;
/** The "search in" choice for the tenant's search index (ranked, every indexed property). */
const ANY = "";
/** Option value of "open the typed text as an id". */
const BY_ID = "__by_id__";

export interface NodeSearchProps {
  /** Called with the chosen hit; `add` when Shift was held (Shift Return / Shift click). */
  onPick: (hit: SearchHit, mode: PickMode) => void;
  placeholder?: string;
  /** Focus with `/` (or ⌘K on Apple devices). Only one search on screen should claim it. */
  hotkey?: boolean;
  /** Show the "Shift Return adds" hint. */
  allowAdd?: boolean;
  /** Keep the chosen node's name in the field (for pickers) instead of clearing it. */
  keepSelection?: boolean;
  width?: number | string;
  /** Offer a "search in" choice of property above the field (for pickers with room for it). */
  scoped?: boolean;
  /** Text the field starts with, e.g. the name of a node picked earlier. */
  initialText?: string;
  /** The user changed the text after picking: what was picked no longer stands. */
  onEdit?: () => void;
}

/** A hit's own id (`properties.id`), falling back to the AGE id. */
function hitId(hit: SearchHit): string {
  return String(hit.properties.id ?? hit.id);
}

/**
 * A search field with a dropdown of ranked hits from the tenant's search
 * index, or of nodes whose `name` (or, when `scoped`, a chosen property)
 * contains the text. When nothing matches, the text can be opened as a
 * node id. Used for the toolbar search and as a node picker in forms.
 *
 * Return always does something sensible: it opens the highlighted hit, or
 * the top hit when none is highlighted, and when the hits for the typed
 * text have not arrived yet it waits for them instead of being lost or
 * opening a hit of an earlier query still on screen.
 */
export function NodeSearch({
  onPick,
  placeholder = "Search",
  hotkey,
  allowAdd,
  keepSelection,
  width = 260,
  scoped,
  initialText = "",
  onEdit,
}: NodeSearchProps) {
  const { api } = useTenantSession();
  const [query, setQuery] = useState(initialText);
  const [debounced] = useDebouncedValue(query, 150);
  const shift = useRef(false);
  const input = useRef<HTMLInputElement>(null);
  const combobox = useCombobox({ onDropdownClose: () => combobox.resetSelectedOption() });
  const schema = useSchema();
  const index = pickSearchIndex(schema.data);
  const properties = usePropertyNames();
  // With a search index, the index by default; without one, `name`.
  const [scope, setScope] = useState<string | null>(null);
  const searchIn = scope ?? (index ? ANY : "name");
  const search = useSearch(
    searchIn === ANY
      ? { q: debounced, index, limit: HIT_LIMIT }
      : { q: debounced, property: searchIn, limit: HIT_LIMIT },
  );
  const hits = debounced.trim() ? (search.data?.nodes ?? []) : [];
  const [idMissing, setIdMissing] = useState<string | null>(null);

  const focus = () => input.current?.focus();
  // ⌘K on Apple devices only: elsewhere Ctrl+K belongs to the browser.
  const keys: HotkeyItem[] = isApple()
    ? [
        ["mod+K", focus],
        ["/", focus],
      ]
    : [["/", focus]];
  useHotkeys(hotkey ? keys : []);

  // The hits shown are for the text in the field, not an earlier query still on screen.
  const fresh =
    query.trim() !== "" &&
    debounced === query &&
    search.data !== undefined &&
    !search.isPlaceholderData &&
    !search.isFetching;
  // Return pressed before the hits arrived: acted on when they do.
  const pending = useRef<PickMode | null>(null);

  const choose = (hit: SearchHit, mode: PickMode) => {
    onPick(hit, mode);
    setQuery(keepSelection ? displayName(hit.properties, hitId(hit)) : "");
    combobox.closeDropdown();
    if (!keepSelection) input.current?.blur();
  };
  const latest = useRef({ hits, choose, combobox });
  latest.current = { hits, choose, combobox };

  // Fresh hits, or the list reopened: open the top hit if Return is waiting,
  // else highlight it so the arrow keys start there.
  useEffect(() => {
    if (!fresh) return;
    const { hits: current, choose: open, combobox: store } = latest.current;
    const mode = pending.current;
    pending.current = null;
    if (current[0] && mode) open(current[0], mode);
    else if (current[0]) store.selectFirstOption();
  }, [fresh, search.data, combobox.dropdownOpened]);

  /** Opens the typed text as a node id, for ids no search matches. */
  const openById = async (id: string, mode: PickMode) => {
    try {
      const node = await api.node(id);
      choose({ id: -1, label: node.label, properties: node.properties, score: null, matched: null }, mode);
    } catch {
      setIdMissing(id);
    }
  };

  const pick = (value: string) => {
    const mode = shift.current ? "add" : "open";
    if (value === BY_ID) void openById(query.trim(), mode);
    const hit = hits.find((h) => String(h.id) === value);
    if (hit) choose(hit, mode);
  };

  /** Return, handled here rather than by the combobox, which ignores it while nothing is highlighted. */
  const submit = (mode: PickMode) => {
    const highlighted = combobox.getSelectedOptionIndex();
    combobox.resetSelectedOption(); // the combobox's own Return handler now does nothing
    if (!fresh) pending.current = mode;
    else if (hits.length > 0) choose(hits[Math.max(highlighted, 0)] ?? hits[0]!, mode);
    else void openById(query.trim(), mode);
  };

  const scopes = [
    ...(index ? [{ value: ANY, label: "Any (index)" }] : []),
    ...(properties.data ?? ["name"]).map((name) => ({ value: name, label: name })),
  ];

  return (
    <Stack gap={4} w={scoped ? width : undefined}>
      {scoped ? (
        <Select
          size="xs"
          variant="filled"
          leftSection={
            <Text size="xs" c="dimmed">
              in
            </Text>
          }
          onFocus={(event) => event.currentTarget.select()}
          radius="md"
          aria-label="Search in"
          title="Search in"
          data={scopes}
          value={searchIn}
          onChange={(next) => setScope(next ?? null)}
          searchable
          selectFirstOptionOnChange
          allowDeselect={false}
          comboboxProps={{ width: 220, position: "bottom-start" }}
        />
      ) : null}
      <Combobox
        store={combobox}
        onOptionSubmit={pick}
        // In a form the list opens under the field; in the toolbar it is wider than the field.
        width={scoped || keepSelection ? "target" : Math.max(Number(width) || 0, 340)}
        position={scoped || keepSelection ? "bottom-start" : "bottom-end"}
      >
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
            rightSection={
              search.isFetching ? <Loader size={12} /> : hotkey ? <Kbd size="xs">{searchShortcut()}</Kbd> : null
            }
            rightSectionWidth={hotkey ? 56 : 28}
            onChange={(event) => {
              setQuery(event.currentTarget.value);
              setIdMissing(null);
              onEdit?.();
              combobox.openDropdown();
              // Nothing is highlighted until the hits for the new text arrive.
              combobox.resetSelectedOption();
            }}
            onFocus={() => query && combobox.openDropdown()}
            onBlur={() => combobox.closeDropdown()}
            onKeyDown={(event) => {
              shift.current = event.shiftKey;
              if (event.key === "Escape") {
                pending.current = null;
                setQuery("");
              }
              if (event.key === "Enter" && !event.nativeEvent.isComposing && query.trim()) {
                event.preventDefault();
                submit(event.shiftKey ? "add" : "open");
              }
            }}
          />
        </Combobox.Target>
        <Combobox.Dropdown hidden={!debounced.trim()}>
          <Combobox.Options>
            <ScrollArea.Autosize mah="min(420px, calc(100vh - 160px))" type="auto">
              {hits.length === 0 && !fresh ? <Combobox.Empty>Searching…</Combobox.Empty> : null}
              {hits.length === 0 && fresh ? (
                idMissing === query.trim() ? (
                  <Combobox.Empty>No node has this name or id</Combobox.Empty>
                ) : (
                  <Combobox.Option value={BY_ID}>
                    <Text size="sm">No matches in {searchIn === ANY ? "the index" : searchIn}</Text>
                    <Text size="xs" c="dimmed">
                      Return opens “{middleEllipsis(query.trim(), ID_CHARS)}” as a node id
                    </Text>
                  </Combobox.Option>
                )
              ) : null}
              {hits.map((hit) => {
                const id = hitId(hit);
                const name = displayName(hit.properties, id);
                return (
                  <Combobox.Option
                    key={hit.id}
                    value={String(hit.id)}
                    title={id}
                    onMouseDown={(event) => {
                      shift.current = event.shiftKey;
                    }}
                  >
                    <Group justify="space-between" wrap="nowrap" gap="sm">
                      <Stack gap={0} miw={0}>
                        <Text size="sm" truncate>
                          {name}
                        </Text>
                        {name !== id ? (
                          <Text size="xs" c="dimmed" ff="monospace">
                            {middleEllipsis(id, ID_CHARS)}
                          </Text>
                        ) : null}
                      </Stack>
                      <Text size="xs" c="dimmed" style={{ flexShrink: 0 }}>
                        {hit.label}
                      </Text>
                    </Group>
                  </Combobox.Option>
                );
              })}
            </ScrollArea.Autosize>
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
    </Stack>
  );
}

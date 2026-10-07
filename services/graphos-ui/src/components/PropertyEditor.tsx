import { ActionIcon, Autocomplete, Group, NumberInput, Switch, Text, TextInput, UnstyledButton } from "@mantine/core";
import { IconPlus, IconX } from "@tabler/icons-react";
import { useState } from "react";
import type { Properties } from "../api/types";
import { formatValue, propertyKind, sortedEntries, SYSTEM_PROPERTIES } from "../lib/properties";
import classes from "./PropertyEditor.module.css";

export interface PropertyEditorProps {
  /** The properties as they are being edited. */
  draft: Properties;
  /** Read-only tokens see the values without inputs. */
  editable: boolean;
  onChange: (draft: Properties) => void;
  /** Property names used elsewhere on this label, offered when adding one. */
  suggestions?: string[];
}

/**
 * A node's properties as grouped rows: name on the left, value on the right,
 * edited in place. Knows nothing about saving; it only changes the draft.
 */
export function PropertyEditor({ draft, editable, onChange, suggestions = [] }: PropertyEditorProps) {
  const set = (key: string, value: unknown) => onChange({ ...draft, [key]: value });
  const remove = (key: string) => {
    const next = { ...draft };
    delete next[key];
    onChange(next);
  };

  return (
    <div className={classes.group}>
      {sortedEntries(draft).map(([key, value]) => {
        const locked = !editable || SYSTEM_PROPERTIES.has(key) || propertyKind(value) === "json";
        return (
          <div key={key} className={classes.row}>
            <Text size="xs" c="dimmed" className={classes.name} title={key}>
              {key}
            </Text>
            {locked ? (
              <Text
                size="sm"
                className={SYSTEM_PROPERTIES.has(key) ? "gx-mono gx-muted" : undefined}
                truncate
                title={formatValue(value)}
              >
                {formatValue(value)}
              </Text>
            ) : (
              <ValueInput name={key} value={value} onChange={(v) => set(key, v)} />
            )}
            {editable && !SYSTEM_PROPERTIES.has(key) ? (
              <ActionIcon
                variant="subtle"
                color="gray"
                size="xs"
                aria-label={`Remove ${key}`}
                className={classes.remove}
                onClick={() => remove(key)}
              >
                <IconX size={12} />
              </ActionIcon>
            ) : (
              <span />
            )}
          </div>
        );
      })}
      {editable ? <NewProperty existing={draft} suggestions={suggestions} onAdd={set} /> : null}
    </div>
  );
}

/** One editable value; the input matches the value's type. */
function ValueInput({ name, value, onChange }: { name: string; value: unknown; onChange: (value: unknown) => void }) {
  const label = `Value of ${name}`;
  switch (propertyKind(value)) {
    case "number":
      return (
        <NumberInput
          variant="unstyled"
          size="sm"
          aria-label={label}
          value={value as number}
          hideControls
          onChange={(v) => onChange(typeof v === "number" ? v : Number(v))}
        />
      );
    case "boolean":
      return (
        <Switch
          size="xs"
          aria-label={label}
          checked={value as boolean}
          onChange={(e) => onChange(e.currentTarget.checked)}
        />
      );
    default:
      return (
        <TextInput
          variant="unstyled"
          size="sm"
          aria-label={label}
          value={formatValue(value)}
          onChange={(e) => onChange(e.currentTarget.value)}
        />
      );
  }
}

/** The last row: a name and a text value for a new property. */
function NewProperty({
  existing,
  suggestions,
  onAdd,
}: {
  existing: Properties;
  suggestions: string[];
  onAdd: (key: string, value: unknown) => void;
}) {
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const taken = key in existing;
  const add = () => {
    if (!key.trim() || taken) return;
    onAdd(key.trim(), value);
    setKey("");
    setValue("");
    setOpen(false);
  };

  if (!open) {
    return (
      <UnstyledButton className={classes.add} onClick={() => setOpen(true)}>
        <Group gap={6}>
          <IconPlus size={13} />
          <Text size="xs">Add property</Text>
        </Group>
      </UnstyledButton>
    );
  }
  return (
    <div className={classes.row}>
      <Autocomplete
        variant="unstyled"
        size="xs"
        placeholder="name"
        aria-label="New property name"
        data={suggestions.filter((name) => !(name in existing))}
        value={key}
        error={taken}
        autoFocus
        onChange={setKey}
        onKeyDown={(e) => e.key === "Enter" && add()}
      />
      <TextInput
        variant="unstyled"
        size="sm"
        placeholder="value"
        aria-label="New property value"
        value={value}
        onChange={(e) => setValue(e.currentTarget.value)}
        onKeyDown={(e) => e.key === "Enter" && add()}
      />
      <ActionIcon variant="subtle" size="xs" aria-label="Add property" onClick={add}>
        <IconPlus size={12} />
      </ActionIcon>
    </div>
  );
}

import { ActionIcon, Autocomplete, Group, NumberInput, Switch, Text, TextInput, UnstyledButton } from "@mantine/core";
import { IconPlus, IconX } from "@tabler/icons-react";
import { useRef, useState } from "react";
import type { Properties } from "../api/types";
import { formatValue, propertyKind, sortedEntries, SYSTEM_PROPERTIES, typedValue } from "../lib/properties";
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
      return <NumberField label={label} value={value as number} onChange={onChange} />;
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

/**
 * A number being edited. What is typed stays as typed ("1.", "-", empty)
 * and the draft only takes it once it is a number, so a half-typed value
 * never becomes 0 or, saved as `NaN`, a deleted property.
 */
function NumberField({ label, value, onChange }: { label: string; value: number; onChange: (value: unknown) => void }) {
  const [text, setText] = useState<string | number>(value);
  const valid = typeof text === "number" || (text !== "" && Number.isFinite(Number(text)));
  return (
    <NumberInput
      variant="unstyled"
      size="sm"
      aria-label={label}
      value={text}
      hideControls
      error={!valid}
      title={valid ? undefined : "Not a number yet; the saved value is unchanged"}
      onChange={(next) => {
        setText(next);
        if (typeof next === "number" && Number.isFinite(next)) onChange(next);
      }}
    />
  );
}

/**
 * The last row: a name and a value for a new property. Return in the name
 * moves to the value (after the suggestion list has filled the name in),
 * Return in the value adds it, Escape cancels. A value that reads exactly as
 * a number or as true/false is stored as one; anything else as text.
 */
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
  const valueInput = useRef<HTMLInputElement>(null);
  const taken = key in existing;
  const cancel = () => {
    setKey("");
    setValue("");
    setOpen(false);
  };
  const add = () => {
    if (!key.trim() || taken) return;
    onAdd(key.trim(), typedValue(value));
    cancel();
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
        onKeyDown={(e) => {
          if (e.key === "Escape") cancel();
          // After the suggestion list has handled Return (and filled the name in).
          if (e.key === "Enter") setTimeout(() => valueInput.current?.focus(), 0);
        }}
      />
      <TextInput
        variant="unstyled"
        size="sm"
        placeholder="value"
        aria-label="New property value"
        ref={valueInput}
        value={value}
        onChange={(e) => setValue(e.currentTarget.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") add();
          if (e.key === "Escape") cancel();
        }}
      />
      <ActionIcon variant="subtle" size="xs" aria-label="Add property" onClick={add}>
        <IconPlus size={12} />
      </ActionIcon>
    </div>
  );
}

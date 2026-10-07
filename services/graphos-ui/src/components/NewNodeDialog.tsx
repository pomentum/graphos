import { Button, Group, Modal, Select, Stack, TextInput } from "@mantine/core";
import { useState } from "react";
import type { NodeUpsert } from "../api/types";

export interface NewNodeDialogProps {
  opened: boolean;
  labels: string[];
  saving: boolean;
  error: string | null;
  onCancel: () => void;
  onCreate: (node: NodeUpsert) => void;
}

/** Asks for a new node's label, id and name. */
export function NewNodeDialog({ opened, labels, saving, error, onCancel, onCreate }: NewNodeDialogProps) {
  const [label, setLabel] = useState<string | null>(labels[0] ?? null);
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const valid = Boolean(label && id.trim());

  const submit = () => {
    if (!valid || !label) return;
    onCreate({ id: id.trim(), label, properties: name.trim() ? { name: name.trim() } : {} });
  };

  return (
    <Modal opened={opened} onClose={onCancel} title="New node" centered radius="lg" size="sm">
      <Stack gap="sm">
        <Select
          label="Label"
          data={labels}
          value={label}
          onChange={setLabel}
          allowDeselect={false}
          searchable
          selectFirstOptionOnChange
        />
        <TextInput
          label="ID"
          description="Unique within the label"
          value={id}
          onChange={(e) => setId(e.currentTarget.value)}
          data-autofocus
        />
        <TextInput
          label="Name"
          value={name}
          onChange={(e) => setName(e.currentTarget.value)}
          error={error}
          onKeyDown={(e) => e.key === "Enter" && submit()}
        />
        <Group justify="flex-end" gap="xs" mt="xs">
          <Button variant="default" size="xs" onClick={onCancel}>
            Cancel
          </Button>
          <Button size="xs" disabled={!valid} loading={saving} onClick={submit}>
            Create
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

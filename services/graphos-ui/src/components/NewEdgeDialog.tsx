import { Button, Group, Input, Modal, Select, Stack, Text } from "@mantine/core";
import { useState } from "react";
import type { SearchHit } from "../api/types";
import { NodeSearch } from "./NodeSearch";

export interface NewEdgeDialogProps {
  opened: boolean;
  /** The node the edge starts from. */
  fromName: string;
  edgeTypes: string[];
  saving: boolean;
  error: string | null;
  onCancel: () => void;
  onCreate: (type: string, to: SearchHit) => void;
}

/** Asks for an edge's type and the node it points to (picked by search). */
export function NewEdgeDialog({ opened, fromName, edgeTypes, saving, error, onCancel, onCreate }: NewEdgeDialogProps) {
  const [type, setType] = useState<string | null>(edgeTypes[0] ?? null);
  const [to, setTo] = useState<SearchHit | null>(null);

  return (
    <Modal opened={opened} onClose={onCancel} title={`Connect ${fromName}`} centered radius="lg" size="sm">
      <Stack gap="sm">
        <Select
          label="Relationship"
          data={edgeTypes}
          value={type}
          onChange={setType}
          allowDeselect={false}
          searchable
          selectFirstOptionOnChange
        />
        <Input.Wrapper label="To" error={error}>
          <NodeSearch placeholder="Find a node" keepSelection width="100%" onPick={(hit) => setTo(hit)} />
        </Input.Wrapper>
        {to ? (
          <Text size="xs" c="dimmed">
            {fromName} → {type} → {String(to.properties.name ?? to.properties.id)}
          </Text>
        ) : null}
        <Group justify="flex-end" gap="xs" mt="xs">
          <Button variant="default" size="xs" onClick={onCancel}>
            Cancel
          </Button>
          <Button size="xs" disabled={!type || !to} loading={saving} onClick={() => type && to && onCreate(type, to)}>
            Connect
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

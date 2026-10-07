import { Button, Group, Input, Modal, Select, Stack, Text } from "@mantine/core";
import { useState } from "react";
import { useTranslation } from "react-i18next";
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
  const { t } = useTranslation();
  const [chosen, setType] = useState<string | null>(null);
  // The first type until one is chosen (the types may arrive after the dialog).
  const type = chosen ?? edgeTypes[0] ?? null;
  const [to, setTo] = useState<SearchHit | null>(null);

  return (
    <Modal
      opened={opened}
      onClose={onCancel}
      title={t("components.newEdgeDialog.title", { name: fromName })}
      centered
      radius="lg"
      size="sm"
    >
      <Stack gap="sm">
        <Select
          label={t("components.newEdgeDialog.relationship")}
          data={edgeTypes}
          value={type}
          onChange={setType}
          allowDeselect={false}
          searchable
          selectFirstOptionOnChange
        />
        <Input.Wrapper label={t("components.newEdgeDialog.to")} error={error}>
          <NodeSearch
            placeholder={t("components.newEdgeDialog.findNode")}
            keepSelection
            scoped
            width="100%"
            onPick={(hit) => setTo(hit)}
          />
        </Input.Wrapper>
        {to ? (
          <Text size="xs" c="dimmed">
            {fromName} → {type} → {String(to.properties.name ?? to.properties.id)}
          </Text>
        ) : null}
        <Group justify="flex-end" gap="xs" mt="xs">
          <Button variant="default" size="xs" onClick={onCancel}>
            {t("common.actions.cancel")}
          </Button>
          <Button size="xs" disabled={!type || !to} loading={saving} onClick={() => type && to && onCreate(type, to)}>
            {t("components.newEdgeDialog.connect")}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

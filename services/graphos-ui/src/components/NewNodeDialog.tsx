import { Alert, Button, Group, Modal, Select, Stack, Text, TextInput } from "@mantine/core";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { NodeUpsert } from "../api/types";
import type { NodeRef } from "../graph/GraphModel";

export interface NewNodeDialogProps {
  opened: boolean;
  labels: string[];
  saving: boolean;
  error: string | null;
  /** A node with the id asked for exists already: shown instead of overwriting it. */
  existing?: NodeRef | null;
  /** The user changed the label or id after `existing` was shown. */
  onEdit?: () => void;
  onOpenExisting?: (ref: NodeRef) => void;
  onCancel: () => void;
  onCreate: (node: NodeUpsert) => void;
}

/**
 * Asks for a new node's label, id and name. When a node with that label and
 * id exists, says so and offers to open it: creating would overwrite it.
 */
export function NewNodeDialog({
  opened,
  labels,
  saving,
  error,
  existing,
  onEdit,
  onOpenExisting,
  onCancel,
  onCreate,
}: NewNodeDialogProps) {
  const { t } = useTranslation();
  const [chosen, setLabel] = useState<string | null>(null);
  // The first label until one is chosen (the labels may arrive after the dialog).
  const label = chosen ?? labels[0] ?? null;
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const valid = Boolean(label && id.trim());

  const submit = () => {
    if (!valid || !label) return;
    onCreate({ id: id.trim(), label, properties: name.trim() ? { name: name.trim() } : {} });
  };

  return (
    <Modal
      opened={opened}
      onClose={onCancel}
      title={t("components.newNodeDialog.title")}
      centered
      radius="lg"
      size="sm"
    >
      <Stack gap="sm">
        <Select
          label={t("components.newNodeDialog.label")}
          data={labels}
          value={label}
          onChange={(next) => {
            setLabel(next);
            onEdit?.();
          }}
          allowDeselect={false}
          searchable
          selectFirstOptionOnChange
        />
        <TextInput
          label={t("components.newNodeDialog.id")}
          description={t("components.newNodeDialog.idDescription")}
          value={id}
          onChange={(e) => {
            setId(e.currentTarget.value);
            onEdit?.();
          }}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          data-autofocus
        />
        <TextInput
          label={t("components.newNodeDialog.name")}
          value={name}
          onChange={(e) => setName(e.currentTarget.value)}
          error={error}
          onKeyDown={(e) => e.key === "Enter" && submit()}
        />
        {existing ? (
          <Alert
            color="orange"
            variant="light"
            radius="md"
            title={t("components.newNodeDialog.existsTitle", { label: existing.label })}
          >
            <Text size="xs">{t("components.newNodeDialog.existsBody")}</Text>
            <Button size="compact-xs" variant="default" mt="xs" onClick={() => onOpenExisting?.(existing)}>
              {t("components.newNodeDialog.openExisting")}
            </Button>
          </Alert>
        ) : null}
        <Group justify="flex-end" gap="xs" mt="xs">
          <Button variant="default" size="xs" onClick={onCancel}>
            {t("common.actions.cancel")}
          </Button>
          <Button size="xs" disabled={!valid || Boolean(existing)} loading={saving} onClick={submit}>
            {t("common.actions.create")}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

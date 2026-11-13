import { Autocomplete, Button, Group, Modal, SegmentedControl, Stack, Text, TextInput } from "@mantine/core";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { NodeRow } from "../../components/NodeTable";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useDeleteNodes, useSetPropertyOnNodes } from "../../data/queries";

/** What the value field holds: text, a number, true or false, or nothing (the property is removed). */
type ValueKind = "text" | "number" | "true" | "false" | "remove";

/** The value to write for `kind`, or why it cannot be written. */
export function bulkValue(kind: ValueKind, text: string): { value: unknown } | { problem: "number" } {
  if (kind === "true") return { value: true };
  if (kind === "false") return { value: false };
  if (kind === "remove") return { value: null };
  if (kind === "number") {
    const n = Number(text.trim());
    return text.trim() && Number.isFinite(n) ? { value: n } : { problem: "number" };
  }
  return { value: text };
}

/**
 * What Browse does to the picked rows at once, for a writer: set (or
 * remove) one property on all of them, or delete them with every
 * relationship touching them, after a confirmation.
 */
export function BulkActions({ rows, names, onDone }: { rows: NodeRow[]; names: string[]; onDone: () => void }) {
  const { t } = useTranslation();
  const remove = useDeleteNodes();
  const setProperty = useSetPropertyOnNodes();
  const [deleting, setDeleting] = useState(false);
  const [setting, setSetting] = useState(false);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<ValueKind>("text");
  const [text, setText] = useState("");
  const parsed = bulkValue(kind, text);
  const reserved = name.trim() === "id";

  const close = () => {
    setSetting(false);
    setName("");
    setText("");
    setKind("text");
  };
  const apply = () => {
    if (!name.trim() || reserved || "problem" in parsed) return;
    setProperty.mutate(
      { nodes: rows, name: name.trim(), value: parsed.value },
      {
        onSuccess: () => {
          close();
          onDone();
        },
      },
    );
  };

  return (
    <>
      <Button size="xs" variant="default" onClick={() => setSetting(true)}>
        {t("views.browse.bulk.set")}
      </Button>
      <Button size="xs" variant="subtle" color="red" onClick={() => setDeleting(true)}>
        {t("views.browse.bulk.delete")}
      </Button>

      <Modal opened={setting} onClose={close} title={t("views.browse.bulk.setTitle", { count: rows.length })} size="sm">
        <Stack gap="sm">
          <Autocomplete
            label={t("views.browse.bulk.property")}
            data={names}
            value={name}
            onChange={setName}
            error={reserved ? t("views.browse.bulk.idReserved") : undefined}
            data-autofocus
          />
          <SegmentedControl
            size="xs"
            value={kind}
            onChange={(value) => setKind(value as ValueKind)}
            data={(["text", "number", "true", "false", "remove"] as const).map((value) => ({
              value,
              label: t(`views.browse.bulk.kinds.${value}`),
            }))}
          />
          {kind === "text" || kind === "number" ? (
            <TextInput
              label={t("views.browse.bulk.value")}
              value={text}
              onChange={(event) => setText(event.currentTarget.value)}
              error={
                kind === "number" && text.trim() && "problem" in parsed ? t("views.browse.bulk.notANumber") : undefined
              }
              onKeyDown={(event) => event.key === "Enter" && apply()}
            />
          ) : null}
          <Text size="xs" c="dimmed">
            {kind === "remove"
              ? t("views.browse.bulk.removeHint", { count: rows.length })
              : t("views.browse.bulk.setHint", { count: rows.length })}
          </Text>
          <Group justify="flex-end" gap="xs">
            <Button size="xs" variant="default" onClick={close}>
              {t("common.actions.cancel")}
            </Button>
            <Button
              size="xs"
              loading={setProperty.isPending}
              disabled={!name.trim() || reserved || "problem" in parsed}
              onClick={apply}
            >
              {kind === "remove" ? t("views.browse.bulk.removeAction") : t("views.browse.bulk.setAction")}
            </Button>
          </Group>
        </Stack>
      </Modal>

      <ConfirmDialog
        opened={deleting}
        title={t("views.browse.bulk.deleteTitle", { count: rows.length })}
        message={t("views.browse.bulk.deleteText", { count: rows.length })}
        confirmLabel={t("common.actions.delete")}
        busy={remove.isPending}
        onConfirm={() =>
          remove.mutate(
            { nodes: rows.map((row) => ({ id: row.id, label: row.label })) },
            {
              onSuccess: () => {
                setDeleting(false);
                onDone();
              },
            },
          )
        }
        onClose={() => setDeleting(false)}
      />
    </>
  );
}

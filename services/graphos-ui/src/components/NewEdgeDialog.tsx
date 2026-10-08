import { Alert, Button, Group, Input, Modal, Select, Stack, Text } from "@mantine/core";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { SearchHit } from "../api/types";
import type { Endpoints } from "../lib/endpoints";
import { NodeSearch } from "./NodeSearch";

export interface NewEdgeDialogProps {
  opened: boolean;
  /** The node the edge starts from. */
  fromName: string;
  /** Its label, to check the schema's declared pairs. */
  fromLabel: string;
  edgeTypes: string[];
  /** The tenant's declared label pairs. */
  endpoints: Endpoints;
  /** graphos refuses edges outside the declared pairs. */
  strict: boolean;
  saving: boolean;
  error: string | null;
  onCancel: () => void;
  onCreate: (type: string, to: SearchHit) => void;
}

/**
 * Asks for an edge's type and the node it points to (picked by search).
 * Types the source label may start (by the schema's declared pairs) come
 * first; the others are hidden when graphos enforces the pairs, listed apart
 * otherwise. The chosen pair is checked once the target is known: refused
 * pairs cannot be sent, undeclared ones are flagged.
 */
export function NewEdgeDialog({
  opened,
  fromName,
  fromLabel,
  edgeTypes,
  endpoints,
  strict,
  saving,
  error,
  onCancel,
  onCreate,
}: NewEdgeDialogProps) {
  const { t } = useTranslation();
  const fitting = edgeTypes.filter((type) => endpoints.startsFrom(type, fromLabel));
  const others = edgeTypes.filter((type) => !fitting.includes(type));
  const offered = strict ? fitting : [...fitting, ...others];
  const [chosen, setType] = useState<string | null>(null);
  // The first type until one is chosen (the types may arrive after the dialog).
  const type = chosen && offered.includes(chosen) ? chosen : (offered[0] ?? null);
  const [to, setTo] = useState<SearchHit | null>(null);
  const coverage = type && to ? endpoints.check(type, fromLabel, to.label) : null;
  const refused = strict && coverage?.status === "undeclared";
  const allowed = type ? endpoints.declared.get(type) : undefined;

  const data =
    others.length && !strict
      ? [
          { group: t("components.newEdgeDialog.fits", { label: fromLabel }), items: fitting },
          { group: t("components.newEdgeDialog.notDeclaredFor", { label: fromLabel }), items: others },
        ].filter((g) => g.items.length)
      : offered;

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
          data={data}
          value={type}
          onChange={setType}
          allowDeselect={false}
          searchable
          selectFirstOptionOnChange
          nothingFoundMessage={t("components.newEdgeDialog.noneDeclared", { label: fromLabel })}
        />
        {strict && offered.length === 0 ? (
          <Text size="xs" c="dimmed">
            {t("components.newEdgeDialog.noneDeclared", { label: fromLabel })}
          </Text>
        ) : null}
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
        {coverage?.status === "undeclared" && type && allowed ? (
          <Alert color={refused ? "red" : "yellow"} variant="light" radius="md">
            <Text size="xs">
              {t(refused ? "components.newEdgeDialog.refused" : "components.newEdgeDialog.undeclared", {
                src: fromLabel,
                type,
                dst: to?.label,
                pairs: allowed.map((p) => `${p.src} → ${p.dst}`).join(", "),
              })}
            </Text>
          </Alert>
        ) : null}
        <Group justify="flex-end" gap="xs" mt="xs">
          <Button variant="default" size="xs" onClick={onCancel}>
            {t("common.actions.cancel")}
          </Button>
          <Button
            size="xs"
            disabled={!type || !to || refused}
            loading={saving}
            onClick={() => type && to && onCreate(type, to)}
          >
            {t("components.newEdgeDialog.connect")}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

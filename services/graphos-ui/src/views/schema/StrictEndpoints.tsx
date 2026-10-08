import { Alert, Badge, Button, Group, List, Modal, Stack, Switch, Text, Tooltip } from "@mantine/core";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { GraphMap, TenantSchema } from "../../api/types";
import { useAddToSchema } from "../../data/queries";
import { formatCount } from "../../lib/time";
import { Endpoints, undeclaredInData } from "../../lib/endpoints";

/** Pairs listed in the confirmation; the rest are counted. */
const SHOWN = 5;

/**
 * Whether graphos refuses edge writes outside the declared label pairs.
 * Administrators switch it; turning it on first says which pairs in the data
 * lie outside the declarations (their edges stay, new ones are refused).
 * Others see a badge while it is on.
 */
export function StrictEndpoints({
  schema,
  map,
  isAdmin,
}: {
  schema: TenantSchema;
  map: GraphMap | undefined;
  isAdmin: boolean;
}) {
  const { t } = useTranslation();
  const save = useAddToSchema();
  const [confirming, setConfirming] = useState(false);
  const strict = Boolean(schema.strict_endpoints);
  const anyDeclared = new Endpoints(schema).any;

  if (!isAdmin) {
    return strict ? (
      <Tooltip label={t("views.schema.strict.hint")} withArrow multiline w={280}>
        <Badge variant="light" radius="sm" tt="none">
          {t("views.schema.strict.on")}
        </Badge>
      </Tooltip>
    ) : null;
  }

  const outside = confirming ? undeclaredInData(schema, map) : [];
  const set = (on: boolean) => save.mutate({ strict_endpoints: on }, { onSuccess: () => setConfirming(false) });

  return (
    <>
      <Tooltip
        label={anyDeclared || strict ? t("views.schema.strict.hint") : t("views.schema.strict.nothingDeclared")}
        withArrow
        multiline
        w={280}
      >
        <Switch
          size="xs"
          label={t("views.schema.strict.label")}
          checked={strict}
          disabled={save.isPending || (!anyDeclared && !strict)}
          onChange={(e) => (e.currentTarget.checked ? setConfirming(true) : set(false))}
        />
      </Tooltip>
      <Modal
        opened={confirming}
        onClose={() => setConfirming(false)}
        title={t("views.schema.strict.confirmTitle")}
        centered
        radius="lg"
      >
        <Stack gap="sm">
          <Text size="sm">{t("views.schema.strict.confirmBody")}</Text>
          {outside.length ? (
            <Alert
              color="orange"
              variant="light"
              radius="md"
              title={t("views.schema.strict.outside", { count: outside.length })}
            >
              <List size="xs" spacing={2}>
                {outside.slice(0, SHOWN).map((p) => (
                  <List.Item key={`${p.type}|${p.src}|${p.dst}`}>
                    {p.src} -[{p.type}]→ {p.dst} ·{" "}
                    {t("common.counts.edges", { count: p.count, formatted: formatCount(p.count) })}
                  </List.Item>
                ))}
              </List>
              {outside.length > SHOWN ? (
                <Text size="xs" mt={4}>
                  {t("views.schema.strict.more", { count: outside.length - SHOWN })}
                </Text>
              ) : null}
            </Alert>
          ) : (
            <Text size="xs" c="dimmed">
              {t("views.schema.strict.allInside")}
            </Text>
          )}
          <Group justify="flex-end" gap="xs">
            <Button size="xs" variant="default" onClick={() => setConfirming(false)}>
              {t("common.actions.cancel")}
            </Button>
            <Button size="xs" loading={save.isPending} onClick={() => set(true)}>
              {t("views.schema.strict.enable")}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
}

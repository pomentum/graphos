import { Alert, Button, Group, Modal, Stack, Text, Tooltip } from "@mantine/core";
import { IconLock } from "@tabler/icons-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useReleaseSchemaManager } from "../../data/queries";
import { useSchemaManager } from "../../data/useSchemaManager";

/**
 * Says who manages the schema, at the top of the Schema page. Others are told
 * to change the structure in that program (styles still change here), and
 * owners can release it, after a confirmation; the manager itself gets a
 * quiet note. Nothing when nobody manages the schema.
 */
export function SchemaManagerBanner() {
  const { t } = useTranslation();
  const { manager, isManager, canRelease } = useSchemaManager();
  const release = useReleaseSchemaManager();
  const [confirming, setConfirming] = useState(false);
  if (manager === null) return null;
  if (isManager) {
    return (
      <Text size="xs" c="dimmed" mb="xs">
        {t("views.schema.manager.yours")}
      </Text>
    );
  }
  return (
    <>
      <Alert
        variant="light"
        color="blue"
        radius="md"
        mb="sm"
        icon={<IconLock size={16} />}
        title={t("views.schema.manager.title", { name: manager })}
      >
        <Group justify="space-between" align="center" gap="sm" wrap="nowrap">
          <Text size="sm">{t("views.schema.manager.others", { name: manager })}</Text>
          {canRelease ? (
            <Button size="xs" variant="default" onClick={() => setConfirming(true)}>
              {t("views.schema.manager.release")}
            </Button>
          ) : null}
        </Group>
      </Alert>
      <Modal
        opened={confirming}
        onClose={() => setConfirming(false)}
        title={t("views.schema.manager.releaseTitle", { name: manager })}
        centered
        radius="lg"
      >
        <Stack gap="sm">
          <Text size="sm">{t("views.schema.manager.releaseBody", { name: manager })}</Text>
          <Group justify="flex-end" gap="xs">
            <Button size="xs" variant="default" onClick={() => setConfirming(false)}>
              {t("common.actions.cancel")}
            </Button>
            <Button
              size="xs"
              color="red"
              loading={release.isPending}
              onClick={() => release.mutate(undefined, { onSuccess: () => setConfirming(false) })}
            >
              {t("views.schema.manager.releaseConfirm")}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
}

/**
 * Wraps a structure control that is disabled because someone else manages
 * the schema, with a tooltip saying who. A disabled button hears no mouse,
 * so the tooltip sits on a wrapper. `reason` is that tooltip's text; the
 * control is shown as it is when it is null.
 */
export function LockedHint({ reason, children }: { reason: string | null; children: ReactNode }) {
  if (!reason) return <>{children}</>;
  return (
    <Tooltip label={reason} withArrow multiline w={260}>
      <span style={{ display: "inline-flex" }}>{children}</span>
    </Tooltip>
  );
}

/** The tooltip of a structure control locked by a schema manager, or null when it is not locked. */
export function useLockedReason(): string | null {
  const { t } = useTranslation();
  const { manager, locked } = useSchemaManager();
  return locked && manager ? t("views.schema.manager.locked", { name: manager }) : null;
}

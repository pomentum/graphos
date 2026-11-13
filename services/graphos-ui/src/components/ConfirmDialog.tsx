import { Button, Group, Modal, Text } from "@mantine/core";
import { useTranslation } from "react-i18next";

/**
 * Asks once before something that cannot be taken back: a title naming what
 * happens, a sentence on what follows, Cancel, and the action in red. For
 * actions behind a menu item or an icon (`ConfirmDelete` is the inline
 * Delete button that asks in a popover).
 */
export function ConfirmDialog({
  opened,
  title,
  message,
  confirmLabel,
  busy = false,
  onConfirm,
  onClose,
}: {
  opened: boolean;
  title: string;
  message: string;
  confirmLabel: string;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Modal opened={opened} onClose={onClose} title={title} size="sm">
      <Text size="sm">{message}</Text>
      <Group justify="flex-end" mt="lg" gap="xs">
        <Button variant="default" size="xs" onClick={onClose}>
          {t("common.actions.cancel")}
        </Button>
        <Button color="red" size="xs" loading={busy} onClick={onConfirm} data-autofocus>
          {confirmLabel}
        </Button>
      </Group>
    </Modal>
  );
}

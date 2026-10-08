import { Button, Group, Modal, PasswordInput, Stack } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/http";
import { useSessionContext } from "../session/SessionProvider";

/** Changing one's own password; every other session of the user ends. */
export function ChangePasswordDialog({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const { changePassword } = useSessionContext();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const close = () => {
    setCurrent("");
    setNext("");
    setAgain("");
    setError(null);
    onClose();
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!current || !next || next !== again) return;
    setBusy(true);
    try {
      await changePassword(current, next);
      notifications.show({ message: t("shell.account.passwordChanged") });
      close();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : t("errors.unreachableTryAgain"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal opened={opened} onClose={close} title={t("shell.account.changePassword")} radius="lg">
      <form onSubmit={(e) => void submit(e)}>
        <Stack gap="sm">
          <PasswordInput
            label={t("shell.signIn.currentPassword")}
            value={current}
            autoFocus
            autoComplete="current-password"
            onChange={(e) => setCurrent(e.currentTarget.value)}
          />
          <PasswordInput
            label={t("shell.signIn.newPassword")}
            description={t("shell.signIn.newPasswordHint")}
            value={next}
            autoComplete="new-password"
            onChange={(e) => setNext(e.currentTarget.value)}
          />
          <PasswordInput
            label={t("shell.signIn.repeatPassword")}
            value={again}
            error={again && again !== next ? t("shell.signIn.mismatch") : error}
            autoComplete="new-password"
            onChange={(e) => setAgain(e.currentTarget.value)}
          />
          <Group justify="flex-end">
            <Button type="submit" size="xs" loading={busy} disabled={!current || !next || next !== again}>
              {t("shell.signIn.changePassword")}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}

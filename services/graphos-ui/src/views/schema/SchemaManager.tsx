import { Alert, Button, Group, Modal, Radio, Select, Stack, Text, TextInput, Tooltip } from "@mantine/core";
import { IconLock } from "@tabler/icons-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useReleaseSchemaManager, useSetSchemaManager, useUsers } from "../../data/queries";
import { useSchemaManager } from "../../data/useSchemaManager";
import { useTenantSession } from "../../session/SessionProvider";

/**
 * Says who manages the schema, at the top of the Schema page. Others are told
 * to change the structure in that program (styles still change here), and
 * owners can release it, after a confirmation, or transfer it; the manager
 * itself gets a quiet note. When nobody manages the schema, owners are offered
 * to name a manager; everyone else sees nothing.
 */
export function SchemaManagerBanner() {
  const { t } = useTranslation();
  const { manager, isManager, canRelease, canName } = useSchemaManager();
  const release = useReleaseSchemaManager();
  const [confirming, setConfirming] = useState(false);
  const [naming, setNaming] = useState(false);
  const dialog = <NameManagerDialog opened={naming} onClose={() => setNaming(false)} />;
  if (manager === null) {
    if (!canName) return null;
    return (
      <>
        <Group justify="space-between" align="center" gap="sm" mb="xs" wrap="nowrap">
          <Text size="xs" c="dimmed">
            {t("views.schema.manager.none")}
          </Text>
          <Button size="xs" variant="default" onClick={() => setNaming(true)}>
            {t("views.schema.manager.name")}
          </Button>
        </Group>
        {dialog}
      </>
    );
  }
  if (isManager) {
    return (
      <>
        <Group justify="space-between" align="center" gap="sm" mb="xs" wrap="nowrap">
          <Text size="xs" c="dimmed">
            {t("views.schema.manager.yours")}
          </Text>
          {canName ? (
            <Button size="xs" variant="subtle" onClick={() => setNaming(true)}>
              {t("views.schema.manager.transfer")}
            </Button>
          ) : null}
        </Group>
        {dialog}
      </>
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
          <Group gap="xs" wrap="nowrap">
            {canName ? (
              <Button size="xs" variant="default" onClick={() => setNaming(true)}>
                {t("views.schema.manager.transfer")}
              </Button>
            ) : null}
            {canRelease ? (
              <Button size="xs" variant="default" onClick={() => setConfirming(true)}>
                {t("views.schema.manager.release")}
              </Button>
            ) : null}
          </Group>
        </Group>
      </Alert>
      {dialog}
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

/** The subject a typed account name stands for: `user:<name>` unless it names its kind. */
function typedSubject(value: string): string {
  const name = value.trim();
  return name.startsWith("user:") || name.startsWith("oidc-sub:") ? name : `user:${name}`;
}

/**
 * Names or transfers the schema's manager (owner): the signed-in user
 * ("self"), or an account, picked from the accounts for an admin, typed for
 * anyone else (only admins list them). The server checks the account exists
 * and is enabled; its refusal is shown as any other.
 */
function NameManagerDialog({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const { me } = useTenantSession();
  const set = useSetSchemaManager();
  const users = useUsers(opened && me.admin);
  const [who, setWho] = useState<"self" | "account">("self");
  const [picked, setPicked] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const accounts = (users.data ?? [])
    .filter((u) => !u.disabled)
    .sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === "service" ? -1 : 1))
    .map((u) => ({ value: `user:${u.name}`, label: u.kind === "service" ? `${u.name} (service)` : u.name }));
  const subject = who === "self" ? "self" : me.admin ? picked : typed.trim() ? typedSubject(typed) : null;
  const close = () => {
    setWho("self");
    setPicked(null);
    setTyped("");
    onClose();
  };
  return (
    <Modal opened={opened} onClose={close} title={t("views.schema.manager.nameTitle")} centered radius="lg">
      <Stack gap="sm">
        <Text size="sm">{t("views.schema.manager.nameBody")}</Text>
        <Radio.Group value={who} onChange={(v) => setWho(v as "self" | "account")}>
          <Stack gap="xs">
            <Radio value="self" label={t("views.schema.manager.me")} />
            <Radio value="account" label={t("views.schema.manager.account")} />
          </Stack>
        </Radio.Group>
        {who === "account" && me.admin ? (
          <Select
            label={t("views.schema.manager.accountPick")}
            data={accounts}
            value={picked}
            onChange={setPicked}
            searchable
            nothingFoundMessage="—"
          />
        ) : null}
        {who === "account" && !me.admin ? (
          <TextInput
            label={t("views.schema.manager.accountName")}
            description={t("views.schema.manager.accountHint")}
            value={typed}
            onChange={(e) => setTyped(e.currentTarget.value)}
          />
        ) : null}
        <Group justify="flex-end" gap="xs">
          <Button size="xs" variant="default" onClick={close}>
            {t("common.actions.cancel")}
          </Button>
          <Button
            size="xs"
            disabled={subject === null}
            loading={set.isPending}
            onClick={() => subject && set.mutate(subject, { onSuccess: close })}
          >
            {t("views.schema.manager.nameConfirm")}
          </Button>
        </Group>
      </Stack>
    </Modal>
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

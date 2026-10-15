import {
  Alert,
  Badge,
  Button,
  Checkbox,
  FileInput,
  Group,
  Loader,
  Modal,
  ScrollArea,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import { IconFileUpload, IconRestore, IconTrash } from "@tabler/icons-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import type { SchemaDocument, UnregisteredTenant } from "../api/types";
import { mayCreate, readFileText, readSchemaFile, SCHEMA_VERSION, TENANT_NAME } from "../lib/schemaFile";
import { relativeTime } from "../lib/time";
import { isActive, useTransfers } from "../lib/transfers";
import { useSessionContext } from "../session/SessionProvider";

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Whether the signed-in user may create some tenant (an admin, or `create_tenants`). */
export function useMayCreateTenants(): boolean {
  const { session } = useSessionContext();
  return Boolean(session && (session.me.admin || session.me.create_tenants.length > 0));
}

/**
 * Creates a tenant: a name, and optionally a schema file (one downloaded from
 * a tenant's Schema page, or written by hand). A name already registered, or
 * kept by an unregistered tenant, is refused before anything is written; the
 * new tenant opens once it exists.
 */
export function NewTenantDialog({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const { session, account, refreshMe } = useSessionContext();
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [doc, setDoc] = useState<SchemaDocument | null>(null);
  const [fileProblem, setFileProblem] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const me = session?.me;
  const trimmed = name.trim();
  const nameError = !trimmed
    ? null
    : !TENANT_NAME.test(trimmed)
      ? t("shell.tenantLife.create.badName")
      : me && !mayCreate(me.create_tenants, me.admin, trimmed)
        ? t("shell.tenantLife.create.mayNotCreate", { name: trimmed })
        : null;

  const close = () => {
    setName("");
    setFile(null);
    setDoc(null);
    setFileProblem(null);
    setError(null);
    onClose();
  };

  const choose = async (next: File | null) => {
    setFile(next);
    setDoc(null);
    setFileProblem(null);
    if (!next) return;
    const read = readSchemaFile(await readFileText(next));
    if ("doc" in read) {
      setDoc(read.doc);
      // The file's own tenant name is a good start for a new one.
      if (!trimmed && TENANT_NAME.test(read.doc.name)) setName(read.doc.name);
    } else if (read.problem.kind === "newerVersion") {
      setFileProblem(t("views.schema.file.newerVersion", { version: read.problem.version, supported: SCHEMA_VERSION }));
    } else {
      setFileProblem(t(`views.schema.file.${read.problem.kind}`));
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!account || !trimmed || nameError) return;
    setBusy(true);
    setError(null);
    try {
      const schema = doc ? { ...doc, name: trimmed } : undefined;
      const preview = await account.previewTenant(trimmed, schema);
      if (preview.exists) setError(t("shell.tenantLife.create.exists", { name: trimmed }));
      else if (preview.restores) setError(t("shell.tenantLife.create.restores", { name: trimmed }));
      else {
        await account.createTenant(trimmed, schema);
        await refreshMe(trimmed);
        close();
      }
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal opened={opened} onClose={close} title={t("shell.tenantLife.create.title")} centered radius="lg">
      <form onSubmit={(event) => void submit(event)}>
        <Stack gap="sm">
          <TextInput
            label={t("shell.tenantLife.create.name")}
            description={t("shell.tenantLife.create.nameHint")}
            value={name}
            onChange={(event) => setName(event.currentTarget.value)}
            error={nameError}
            data-autofocus
          />
          <FileInput
            label={t("shell.tenantLife.create.schema")}
            description={t("shell.tenantLife.create.schemaHint")}
            accept=".json,application/json"
            leftSection={<IconFileUpload size={16} />}
            value={file}
            onChange={(next) => void choose(next)}
            error={fileProblem}
            clearable
          />
          {error ? (
            <Alert color="red" variant="light" radius="md">
              {error}
            </Alert>
          ) : null}
          <Group justify="flex-end" gap="xs">
            <Button variant="default" onClick={close}>
              {t("common.actions.cancel")}
            </Button>
            <Button type="submit" loading={busy} disabled={!trimmed || Boolean(nameError) || Boolean(fileProblem)}>
              {t("shell.tenantLife.create.create")}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}

/**
 * Deletes a tenant. A registered one is unregistered by default: its data,
 * schema and access stay and it can be restored; with "also delete its data"
 * (the only choice for an unregistered one) it is deleted for good, after its
 * name is typed. Refused while a dataset transfer of it runs here.
 */
export function DeleteTenantDialog({
  name,
  registered,
  opened,
  onClose,
  onDone,
}: {
  name: string;
  /** Registered (unregister, or delete with its data), or unregistered (delete its data). */
  registered: boolean;
  opened: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const { account } = useSessionContext();
  const [dropData, setDropData] = useState(!registered);
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const transferring = useTransfers().some((x) => x.tenant === name && isActive(x));
  const confirmed = !dropData || typed === name;

  const close = () => {
    setDropData(!registered);
    setTyped("");
    setError(null);
    onClose();
  };

  const submit = async () => {
    if (!account || !confirmed) return;
    setBusy(true);
    setError(null);
    try {
      await account.deleteTenant(name, dropData);
      close();
      onDone();
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      opened={opened}
      onClose={close}
      title={t(registered ? "shell.tenantLife.delete.title" : "shell.tenantLife.delete.titleData", { name })}
      centered
      radius="lg"
    >
      <Stack gap="sm">
        {registered ? (
          <>
            <Text size="sm">{t("shell.tenantLife.delete.unregisterWhat")}</Text>
            <Checkbox
              label={t("shell.tenantLife.delete.dropData")}
              checked={dropData}
              onChange={(event) => setDropData(event.currentTarget.checked)}
            />
          </>
        ) : null}
        {dropData ? (
          <>
            <Alert color="red" variant="light" radius="md">
              {t("shell.tenantLife.delete.dropDataWhat")}
            </Alert>
            <TextInput
              label={t("shell.tenantLife.delete.typeName", { name })}
              value={typed}
              onChange={(event) => setTyped(event.currentTarget.value)}
              autoComplete="off"
              data-autofocus
            />
          </>
        ) : null}
        {transferring ? (
          <Text size="xs" c="dimmed">
            {t("shell.tenantLife.delete.transferRunning")}
          </Text>
        ) : null}
        {error ? (
          <Alert color="red" variant="light" radius="md">
            {error}
          </Alert>
        ) : null}
        <Group justify="flex-end" gap="xs">
          <Button variant="default" onClick={close}>
            {t("common.actions.cancel")}
          </Button>
          <Button
            color="red"
            loading={busy}
            disabled={!confirmed || transferring}
            onClick={() => void submit()}
            leftSection={<IconTrash size={14} />}
          >
            {dropData ? t("shell.tenantLife.delete.deleteForGood") : t("shell.tenantLife.delete.unregister")}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

/**
 * Tenants unregistered with their data kept, that the user owns (every one,
 * for an admin): restore one as it was, or delete it with its data.
 */
export function UnregisteredTenantsDialog({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const { account, refreshMe } = useSessionContext();
  const [list, setList] = useState<UnregisteredTenant[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [restoring, setRestoring] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!account) return;
    setError(null);
    try {
      setList(await account.unregisteredTenants());
    } catch (failure) {
      setError(message(failure));
      setList([]);
    }
  }, [account]);

  useEffect(() => {
    if (opened) void load();
    else setList(null);
  }, [opened, load]);

  const restore = async (name: string) => {
    if (!account) return;
    setRestoring(name);
    setError(null);
    try {
      await account.restoreTenant(name);
      await refreshMe(name);
      onClose();
    } catch (failure) {
      setError(message(failure));
    } finally {
      setRestoring(null);
    }
  };

  return (
    <>
      <Modal opened={opened} onClose={onClose} title={t("shell.tenantLife.list.title")} centered radius="lg" size="lg">
        <Stack gap="sm">
          <Text size="xs" c="dimmed">
            {t("shell.tenantLife.list.hint")}
          </Text>
          {error ? (
            <Alert color="red" variant="light" radius="md">
              {error}
            </Alert>
          ) : null}
          {list === null ? (
            <Loader size="sm" />
          ) : list.length === 0 ? (
            <Text size="sm">{t("shell.tenantLife.list.empty")}</Text>
          ) : (
            <ScrollArea.Autosize mah="min(420px, calc(100vh - 260px))" type="auto">
              <Stack gap="xs">
                {list.map((tenant) => (
                  <Group key={tenant.name} justify="space-between" wrap="nowrap" gap="sm" align="flex-start">
                    <div style={{ minWidth: 0 }}>
                      <Group gap={6} wrap="nowrap">
                        <Text size="sm" fw={600} truncate title={tenant.name}>
                          {tenant.name}
                        </Text>
                        {tenant.kind === "orphan" ? (
                          <Badge size="xs" variant="light" color="gray">
                            {t("shell.tenantLife.list.orphanBadge")}
                          </Badge>
                        ) : null}
                      </Group>
                      <Text size="xs" c="dimmed">
                        {tenant.kind === "orphan"
                          ? t("shell.tenantLife.list.orphan")
                          : [
                              t("shell.tenantLife.list.labels", {
                                vertex: tenant.vertex_labels,
                                edge: tenant.edge_labels,
                              }),
                              tenant.unregistered_at
                                ? t("shell.tenantLife.list.by", {
                                    when: relativeTime(tenant.unregistered_at),
                                    who: tenant.unregistered_by ?? t("common.unknownConsumer"),
                                  })
                                : null,
                            ]
                              .filter(Boolean)
                              .join(" · ")}
                      </Text>
                    </div>
                    <Group gap={4} wrap="nowrap" style={{ flex: "none" }}>
                      {tenant.kind === "unregistered" ? (
                        <Button
                          size="compact-xs"
                          variant="light"
                          leftSection={<IconRestore size={13} />}
                          loading={restoring === tenant.name}
                          onClick={() => void restore(tenant.name)}
                        >
                          {t("shell.tenantLife.list.restore")}
                        </Button>
                      ) : null}
                      <Button
                        size="compact-xs"
                        variant="subtle"
                        color="red"
                        leftSection={<IconTrash size={13} />}
                        onClick={() => setDeleting(tenant.name)}
                      >
                        {t("shell.tenantLife.list.deleteData")}
                      </Button>
                    </Group>
                  </Group>
                ))}
              </Stack>
            </ScrollArea.Autosize>
          )}
        </Stack>
      </Modal>
      {deleting ? (
        <DeleteTenantDialog
          name={deleting}
          registered={false}
          opened
          onClose={() => setDeleting(null)}
          onDone={() => void load()}
        />
      ) : null}
    </>
  );
}

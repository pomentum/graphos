import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Code,
  CopyButton,
  Group,
  Menu,
  Modal,
  SegmentedControl,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import { IconCheck, IconCopy, IconDots, IconPlus, IconX } from "@tabler/icons-react";
import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import type { Grant, GrantLevel, UserCreated, UserInfo } from "../../api/types";
import { useAccessMutations, useAuthConfig, useGrants, useUsers } from "../../data/queries";
import { relativeTime } from "../../lib/time";
import { useTenantSession } from "../../session/SessionProvider";
import classes from "./AccessView.module.css";
import { parseSubject, subjectKinds, subjectOf, type SubjectKind } from "./subjects";

const TENANT_LEVELS: GrantLevel[] = ["read", "write", "maintain", "owner"];
const ALL_LEVELS: GrantLevel[] = [...TENANT_LEVELS, "create_tenants", "admin"];

/**
 * Access: who may do what. Owners of this tenant manage who has access to it;
 * admins also manage users and service accounts, and every grant (patterns
 * such as `sol_*`, the right to create tenants, admin).
 */
export function AccessView() {
  const { t } = useTranslation();
  const { tenant, isAdmin } = useTenantSession();
  return (
    <div className={classes.root}>
      <Stack gap="xl">
        <TenantAccess tenant={tenant} />
        {isAdmin ? <Users /> : null}
        {isAdmin ? <AllGrants /> : null}
        {!isAdmin ? (
          <Text size="xs" c="dimmed">
            {t("views.access.ownerHint")}
          </Text>
        ) : null}
      </Stack>
    </div>
  );
}

function subjectName(subject: string): string {
  return parseSubject(subject).name;
}

/** Who a grant is for: the name, and for a single sign-on identity what it is. */
function SubjectLabel({ subject }: { subject: string }) {
  const { t } = useTranslation();
  const { kind, name } = parseSubject(subject);
  return (
    <Group gap={6} wrap="nowrap">
      <span>{name}</span>
      {kind === "user" ? null : (
        <Badge variant="outline" color="gray" radius="sm" size="xs" tt="none">
          {t(`views.access.subjects.${kind}`)}
        </Badge>
      )}
    </Group>
  );
}

/** Whether single sign-on is set up, so its identities can be granted. */
function useSso(): boolean {
  return Boolean(useAuthConfig().data?.oidc);
}

/** What the grant is for: a graphos user or, with single sign-on, a group,
 *  role, person or program of the identity provider. */
function KindSelect({ kind, onChange }: { kind: SubjectKind; onChange: (kind: SubjectKind) => void }) {
  const { t } = useTranslation();
  return (
    <Select
      size="xs"
      label={t("views.access.form.kind")}
      data={subjectKinds(true).map((value) => ({ value, label: t(`views.access.subjects.${value}`) }))}
      value={kind}
      onChange={(value) => value && onChange(value as SubjectKind)}
      allowDeselect={false}
      w={150}
    />
  );
}

/** The grants that apply to this tenant, and adding one (by user name, or a
 *  single sign-on identity). */
function TenantAccess({ tenant }: { tenant: string }) {
  const { t } = useTranslation();
  const { isAdmin } = useTenantSession();
  const grants = useGrants(tenant);
  const sso = useSso();
  const { grant, revoke } = useAccessMutations();
  const [kind, setKind] = useState<SubjectKind>("user");
  const [user, setUser] = useState("");
  const [level, setLevel] = useState<GrantLevel>("read");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!user.trim()) return;
    grant.mutate({ subject: subjectOf(kind, user), tenant, level }, { onSuccess: () => setUser("") });
  };

  return (
    <section>
      <Title order={2} size="h4" mb={4}>
        {t("views.access.tenantTitle", { tenant })}
      </Title>
      <Text size="sm" c="dimmed" mb="sm">
        {t("views.access.tenantHint")}
      </Text>
      <div className={classes.group}>
        <Table verticalSpacing={6} highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t("views.access.columns.who")}</Table.Th>
              <Table.Th>{t("views.access.columns.level")}</Table.Th>
              <Table.Th>{t("views.access.columns.through")}</Table.Th>
              <Table.Th w={40} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {(grants.data ?? []).map((g) => {
              // A pattern grant (sol_*, *) is an admin's to take back.
              const revocable = g.tenant === tenant || isAdmin;
              return (
                <Table.Tr key={`${g.subject}|${g.tenant}|${g.level}`}>
                  <Table.Td>
                    <SubjectLabel subject={g.subject} />
                  </Table.Td>
                  <Table.Td>
                    <LevelBadge level={g.level} />
                  </Table.Td>
                  <Table.Td>
                    <Text size="xs" c="dimmed">
                      {g.tenant === tenant ? t("views.access.direct") : g.tenant}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    {revocable ? (
                      <RevokeButton grant={g} busy={revoke.isPending} onRevoke={() => revoke.mutate(g)} />
                    ) : null}
                  </Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
        {grants.data?.length === 0 ? (
          <Text size="sm" c="dimmed" p="sm">
            {t("views.access.noGrants")}
          </Text>
        ) : null}
      </div>
      <form onSubmit={submit}>
        <Group gap="xs" mt="sm" align="flex-end" wrap="wrap">
          {sso ? <KindSelect kind={kind} onChange={setKind} /> : null}
          <TextInput
            size="xs"
            label={kind === "user" ? t("views.access.form.user") : t("views.access.form.name")}
            placeholder={t(`views.access.form.placeholders.${kind}`)}
            value={user}
            onChange={(e) => setUser(e.currentTarget.value)}
            w={200}
          />
          <Select
            size="xs"
            label={t("views.access.form.level")}
            data={TENANT_LEVELS.map((value) => ({ value, label: t(`views.access.levels.${value}`) }))}
            value={level}
            onChange={(value) => value && setLevel(value as GrantLevel)}
            allowDeselect={false}
            w={160}
          />
          <Button size="xs" type="submit" leftSection={<IconPlus size={14} />} loading={grant.isPending}>
            {t("views.access.form.give")}
          </Button>
        </Group>
      </form>
      {sso ? (
        <Text size="xs" c="dimmed" mt={6}>
          {t("views.access.form.ssoHint")}
        </Text>
      ) : null}
    </section>
  );
}

/** People and service accounts (admins). */
function Users() {
  const { t } = useTranslation();
  const users = useUsers(true);
  const { createUser, resetSecret, setDisabled, deleteUser } = useAccessMutations();
  const { me } = useTenantSession();
  const [adding, setAdding] = useState(false);
  const [shown, setShown] = useState<UserCreated | null>(null);

  const reset = (user: UserInfo) => resetSecret.mutate(user.name, { onSuccess: setShown });

  return (
    <section>
      <Group justify="space-between" mb={4}>
        <Title order={2} size="h4">
          {t("views.access.usersTitle")}
        </Title>
        <Button size="xs" variant="default" leftSection={<IconPlus size={14} />} onClick={() => setAdding(true)}>
          {t("views.access.addUser")}
        </Button>
      </Group>
      <Text size="sm" c="dimmed" mb="sm">
        {t("views.access.usersHint")}
      </Text>
      <div className={classes.group}>
        <Table verticalSpacing={6} highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t("views.access.columns.name")}</Table.Th>
              <Table.Th>{t("views.access.columns.kind")}</Table.Th>
              <Table.Th>{t("views.access.columns.status")}</Table.Th>
              <Table.Th>{t("views.access.columns.lastSignIn")}</Table.Th>
              <Table.Th w={40} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {(users.data ?? []).map((user) => (
              <Table.Tr key={user.name}>
                <Table.Td>{user.name}</Table.Td>
                <Table.Td>
                  <Badge variant="light" color="gray" radius="sm" tt="none">
                    {t(`views.access.kinds.${user.kind}`)}
                  </Badge>
                </Table.Td>
                <Table.Td>
                  <Text size="xs" c={user.disabled ? "red" : "dimmed"}>
                    {user.disabled
                      ? t("views.access.status.disabled")
                      : user.must_change
                        ? t("views.access.status.mustChange")
                        : t("views.access.status.active")}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Text size="xs" c="dimmed">
                    {user.last_login_at ? relativeTime(user.last_login_at) : t("views.access.never")}
                  </Text>
                </Table.Td>
                <Table.Td>
                  {user.name === me.consumer ? null : (
                    <Menu position="bottom-end" withinPortal>
                      <Menu.Target>
                        <ActionIcon
                          variant="subtle"
                          color="gray"
                          aria-label={t("views.access.actionsFor", { name: user.name })}
                        >
                          <IconDots size={16} />
                        </ActionIcon>
                      </Menu.Target>
                      <Menu.Dropdown>
                        <Menu.Item onClick={() => reset(user)}>
                          {user.kind === "service" ? t("views.access.newSecret") : t("views.access.resetPassword")}
                        </Menu.Item>
                        <Menu.Item onClick={() => setDisabled.mutate({ name: user.name, disabled: !user.disabled })}>
                          {user.disabled ? t("views.access.enable") : t("views.access.disable")}
                        </Menu.Item>
                        <Menu.Divider />
                        <Menu.Item color="red" onClick={() => deleteUser.mutate(user.name)}>
                          {t("views.access.delete")}
                        </Menu.Item>
                      </Menu.Dropdown>
                    </Menu>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </div>
      <AddUserDialog
        opened={adding}
        busy={createUser.isPending}
        onClose={() => setAdding(false)}
        onCreate={(name, kind) =>
          createUser.mutate(
            { name, kind },
            {
              onSuccess: (created) => {
                setAdding(false);
                setShown(created);
              },
            },
          )
        }
      />
      <SecretDialog created={shown} onClose={() => setShown(null)} />
    </section>
  );
}

function AddUserDialog({
  opened,
  busy,
  onClose,
  onCreate,
}: {
  opened: boolean;
  busy: boolean;
  onClose: () => void;
  onCreate: (name: string, kind: "person" | "service") => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"person" | "service">("person");
  const valid = /^[a-z][a-z0-9_.-]{0,62}$/.test(name);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (valid) onCreate(name, kind);
  };
  return (
    <Modal opened={opened} onClose={onClose} title={t("views.access.addUser")} radius="lg">
      <form onSubmit={submit}>
        <Stack gap="sm">
          <SegmentedControl
            size="xs"
            value={kind}
            onChange={(value) => setKind(value as "person" | "service")}
            data={[
              { value: "person", label: t("views.access.kinds.person") },
              { value: "service", label: t("views.access.kinds.service") },
            ]}
          />
          <Text size="xs" c="dimmed">
            {kind === "person" ? t("views.access.personHint") : t("views.access.serviceHint")}
          </Text>
          <TextInput
            label={t("views.access.columns.name")}
            description={t("views.access.nameRule")}
            value={name}
            autoFocus
            error={name && !valid ? t("views.access.nameRule") : null}
            onChange={(e) => setName(e.currentTarget.value.toLowerCase())}
          />
          <Group justify="flex-end">
            <Button type="submit" size="xs" loading={busy} disabled={!valid}>
              {t("views.access.create")}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}

/** The password or secret of a user just created or reset: shown once. */
function SecretDialog({ created, onClose }: { created: UserCreated | null; onClose: () => void }) {
  const { t } = useTranslation();
  const service = created?.user.kind === "service";
  return (
    <Modal opened={created !== null} onClose={onClose} title={t("views.access.secretTitle")} radius="lg">
      {created ? (
        <Stack gap="sm">
          <Alert color="yellow" variant="light" p="xs">
            {service ? t("views.access.secretOnceService") : t("views.access.secretOncePerson")}
          </Alert>
          <Text size="sm">{created.user.name}</Text>
          <Group gap="xs" wrap="nowrap">
            <Code className={classes.secret}>{created.password}</Code>
            <CopyButton value={created.password}>
              {({ copied, copy }) => (
                <Tooltip label={copied ? t("views.access.copied") : t("views.access.copy")} withArrow>
                  <ActionIcon variant="subtle" onClick={copy} aria-label={t("views.access.copy")}>
                    {copied ? <IconCheck size={16} /> : <IconCopy size={16} />}
                  </ActionIcon>
                </Tooltip>
              )}
            </CopyButton>
          </Group>
          <Group justify="flex-end">
            <Button size="xs" onClick={onClose}>
              {t("views.access.done")}
            </Button>
          </Group>
        </Stack>
      ) : null}
    </Modal>
  );
}

/** Every grant, with patterns and global rights (admins). */
function AllGrants() {
  const { t } = useTranslation();
  const grants = useGrants(null);
  const users = useUsers(true);
  const sso = useSso();
  const { grant, revoke } = useAccessMutations();
  const [kind, setKind] = useState<SubjectKind>("user");
  const [user, setUser] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [pattern, setPattern] = useState("*");
  const [level, setLevel] = useState<GrantLevel>("read");
  const who = kind === "user" ? (user ?? "") : name;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!who.trim() || !pattern.trim()) return;
    grant.mutate(
      { subject: subjectOf(kind, who), tenant: level === "admin" ? "*" : pattern.trim(), level },
      { onSuccess: () => setName("") },
    );
  };

  return (
    <section>
      <Title order={2} size="h4" mb={4}>
        {t("views.access.allTitle")}
      </Title>
      <Text size="sm" c="dimmed" mb="sm">
        {t("views.access.allHint")}
      </Text>
      <div className={classes.group}>
        <Table verticalSpacing={6} highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t("views.access.columns.who")}</Table.Th>
              <Table.Th>{t("views.access.columns.tenants")}</Table.Th>
              <Table.Th>{t("views.access.columns.level")}</Table.Th>
              <Table.Th>{t("views.access.columns.grantedBy")}</Table.Th>
              <Table.Th w={40} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {(grants.data ?? []).map((g) => (
              <Table.Tr key={`${g.subject}|${g.tenant}|${g.level}`}>
                <Table.Td>
                  <SubjectLabel subject={g.subject} />
                </Table.Td>
                <Table.Td>
                  <Code>{g.tenant}</Code>
                </Table.Td>
                <Table.Td>
                  <LevelBadge level={g.level} />
                </Table.Td>
                <Table.Td>
                  <Text size="xs" c="dimmed">
                    {g.granted_by ?? ""}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <RevokeButton grant={g} busy={revoke.isPending} onRevoke={() => revoke.mutate(g)} />
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </div>
      <form onSubmit={submit}>
        <Group gap="xs" mt="sm" align="flex-end" wrap="wrap">
          {sso ? <KindSelect kind={kind} onChange={setKind} /> : null}
          {kind === "user" ? (
            <Select
              size="xs"
              label={t("views.access.form.user")}
              data={(users.data ?? []).map((u) => u.name)}
              value={user}
              onChange={setUser}
              searchable
              w={180}
            />
          ) : (
            <TextInput
              size="xs"
              label={t("views.access.form.name")}
              placeholder={t(`views.access.form.placeholders.${kind}`)}
              value={name}
              onChange={(e) => setName(e.currentTarget.value)}
              w={180}
            />
          )}
          <TextInput
            size="xs"
            label={t("views.access.form.tenants")}
            description={t("views.access.form.tenantsHint")}
            value={level === "admin" ? "*" : pattern}
            disabled={level === "admin"}
            onChange={(e) => setPattern(e.currentTarget.value)}
            w={160}
          />
          <Select
            size="xs"
            label={t("views.access.form.level")}
            data={ALL_LEVELS.map((value) => ({ value, label: t(`views.access.levels.${value}`) }))}
            value={level}
            onChange={(value) => value && setLevel(value as GrantLevel)}
            allowDeselect={false}
            w={170}
          />
          <Button
            size="xs"
            type="submit"
            leftSection={<IconPlus size={14} />}
            loading={grant.isPending}
            disabled={!who.trim()}
          >
            {t("views.access.form.give")}
          </Button>
        </Group>
      </form>
      {sso ? (
        <Text size="xs" c="dimmed" mt={6}>
          {t("views.access.form.ssoHint")}
        </Text>
      ) : null}
    </section>
  );
}

function LevelBadge({ level }: { level: GrantLevel }) {
  const { t } = useTranslation();
  const strong = level === "admin" || level === "owner";
  return (
    <Badge variant={strong ? "filled" : "light"} color={strong ? "accent" : "gray"} radius="sm" tt="none">
      {t(`views.access.levels.${level}`)}
    </Badge>
  );
}

function RevokeButton({ grant, busy, onRevoke }: { grant: Grant; busy: boolean; onRevoke: () => void }) {
  const { t } = useTranslation();
  return (
    <Tooltip label={t("views.access.revoke")} withArrow>
      <ActionIcon
        variant="subtle"
        color="red"
        loading={busy}
        onClick={onRevoke}
        aria-label={t("views.access.revokeFrom", { name: subjectName(grant.subject), level: grant.level })}
      >
        <IconX size={14} />
      </ActionIcon>
    </Tooltip>
  );
}

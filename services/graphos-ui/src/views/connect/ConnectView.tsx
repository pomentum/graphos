import {
  Alert,
  Button,
  Divider,
  Group,
  Paper,
  PasswordInput,
  ScrollArea,
  Stack,
  Text,
  TextInput,
  Title,
  UnstyledButton,
} from "@mantine/core";
import { IconChevronRight, IconPlus, IconRestore, IconSearch } from "@tabler/icons-react";
import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { ApiError } from "../../api/http";
import { NewTenantDialog, UnregisteredTenantsDialog, useMayCreateTenants } from "../../components/TenantLifecycle";
import { useSessionContext } from "../../session/SessionProvider";
import classes from "./ConnectView.module.css";

/** Above this many tenants the list offers a search box. */
const TENANT_SEARCH_FROM = 8;

/**
 * Sign-in: through the identity provider when single sign-on is configured
 * (a page load already tried it silently), or with a graphos user name and
 * password; then a new password when it must change (the first sign-in, or
 * after a reset), then a tenant when the user has more than one. Nothing is
 * kept in the browser: a reload comes back here, or signs in again silently.
 */
export function ConnectView() {
  const { session } = useSessionContext();
  return (
    <div className={classes.root}>
      <Paper radius="xl" p="xl" className={classes.card}>
        <Stack gap="lg">
          <div className={classes.mark} aria-hidden="true" />
          {!session ? <SignInForm /> : session.me.must_change ? <ChangePasswordForm /> : <TenantChooser />}
        </Stack>
      </Paper>
    </div>
  );
}

function SignInForm() {
  const { t } = useTranslation();
  const { signIn, notice, config, signInWithSso } = useSessionContext();
  const [leaving, setLeaving] = useState(false);
  const sso = config?.oidc ?? null;
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!user.trim() || !password) return;
    setBusy(true);
    try {
      await signIn(user.trim(), password);
      setError(null);
    } catch (e) {
      // Kept until the next answer, so the button does not move under the pointer.
      setError(
        e instanceof ApiError && e.status === 401
          ? t("errors.signInRefused")
          : e instanceof ApiError && e.status === 429
            ? t("errors.signInTooMany")
            : t("errors.unreachableTryAgain"),
      );
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(e) => void submit(e)}>
      <Stack gap="md">
        <div>
          <Title order={1} size="h3">
            {t("shell.signIn.title")}
          </Title>
          <Text size="sm" c="dimmed">
            {sso ? t("shell.signIn.hintSso") : t("shell.signIn.hint")}
          </Text>
        </div>
        {notice ? (
          <Alert color="gray" variant="light" p="xs">
            {notice === "ssoFailed" ? t("shell.signIn.ssoFailed") : t("shell.signIn.sessionEnded")}
          </Alert>
        ) : null}
        {sso ? (
          <>
            <Button
              variant="filled"
              loading={leaving}
              onClick={() => {
                setLeaving(true);
                signInWithSso().catch(() => {
                  setLeaving(false);
                  setError(t("errors.unreachableTryAgain"));
                });
              }}
            >
              {t("shell.signIn.withSso", { name: sso.display_name })}
            </Button>
            <Divider label={t("shell.signIn.orPassword")} labelPosition="center" />
          </>
        ) : null}
        <TextInput
          label={t("shell.signIn.user")}
          value={user}
          autoFocus
          autoComplete="username"
          onChange={(e) => setUser(e.currentTarget.value)}
        />
        <PasswordInput
          label={t("shell.signIn.password")}
          value={password}
          error={error}
          autoComplete="current-password"
          onChange={(e) => setPassword(e.currentTarget.value)}
        />
        <Button type="submit" variant={sso ? "default" : "filled"} loading={busy} disabled={!user.trim() || !password}>
          {t("shell.signIn.continue")}
        </Button>
      </Stack>
    </form>
  );
}

function ChangePasswordForm() {
  const { t } = useTranslation();
  const { session, changePassword, signOut } = useSessionContext();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mismatch = again.length > 0 && again !== next;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!current || !next || next !== again) return;
    setBusy(true);
    try {
      await changePassword(current, next);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : t("errors.unreachableTryAgain"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(e) => void submit(e)}>
      <Stack gap="md">
        <div>
          <Title order={1} size="h3">
            {t("shell.signIn.changeTitle")}
          </Title>
          <Text size="sm" c="dimmed">
            {t("shell.signIn.changeHint", { consumer: session?.me.consumer ?? "" })}
          </Text>
        </div>
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
          error={mismatch ? t("shell.signIn.mismatch") : error}
          autoComplete="new-password"
          onChange={(e) => setAgain(e.currentTarget.value)}
        />
        <Button type="submit" loading={busy} disabled={!current || !next || next !== again}>
          {t("shell.signIn.changePassword")}
        </Button>
        <Button variant="subtle" color="gray" size="xs" onClick={signOut}>
          {t("shell.signIn.signOut")}
        </Button>
      </Stack>
    </form>
  );
}

function TenantChooser() {
  const { t } = useTranslation();
  const { session, selectTenant, signOut } = useSessionContext();
  const mayCreate = useMayCreateTenants();
  const [creating, setCreating] = useState(false);
  const [listing, setListing] = useState(false);
  const [query, setQuery] = useState("");
  const tenants = session?.me.tenants ?? [];
  const needle = query.trim().toLowerCase();
  const shown = needle ? tenants.filter((tenant) => tenant.toLowerCase().includes(needle)) : tenants;

  return (
    <>
      <div>
        <Title order={1} size="h3">
          {t("shell.signIn.chooseTenant")}
        </Title>
        <Text size="sm" c="dimmed">
          {t("shell.signIn.signedInAs", { consumer: session?.me.consumer ?? "" })}
        </Text>
      </div>
      {tenants.length === 0 ? (
        <Text size="sm">{t("shell.signIn.noTenants")}</Text>
      ) : (
        <Stack gap="xs">
          {tenants.length > TENANT_SEARCH_FROM ? (
            <TextInput
              size="xs"
              placeholder={t("shell.tenants.findAmong", { count: tenants.length })}
              aria-label={t("shell.tenants.find")}
              leftSection={<IconSearch size={14} />}
              value={query}
              autoFocus
              onChange={(e) => setQuery(e.currentTarget.value)}
              onKeyDown={(e) => e.key === "Enter" && shown[0] && selectTenant(shown[0])}
            />
          ) : null}
          <ScrollArea.Autosize mah="min(360px, calc(100vh - 320px))" type="auto" className={classes.list}>
            {shown.map((tenant) => (
              <UnstyledButton key={tenant} className={classes.tenant} onClick={() => selectTenant(tenant)}>
                <Text size="sm" truncate title={tenant}>
                  {tenant}
                </Text>
                <IconChevronRight size={14} className="gx-muted" />
              </UnstyledButton>
            ))}
            {shown.length === 0 ? (
              <Text size="sm" c="dimmed" className={classes.tenant}>
                {t("shell.tenants.noMatch")}
              </Text>
            ) : null}
          </ScrollArea.Autosize>
        </Stack>
      )}
      <Group gap="xs" justify="center">
        {mayCreate ? (
          <Button variant="light" size="xs" leftSection={<IconPlus size={14} />} onClick={() => setCreating(true)}>
            {t("shell.tenantLife.newTenant")}
          </Button>
        ) : null}
        <Button
          variant="subtle"
          color="gray"
          size="xs"
          leftSection={<IconRestore size={14} />}
          onClick={() => setListing(true)}
        >
          {t("shell.tenantLife.unregistered")}
        </Button>
      </Group>
      <Button variant="subtle" color="gray" size="xs" onClick={signOut}>
        {t("shell.signIn.signOut")}
      </Button>
      <NewTenantDialog opened={creating} onClose={() => setCreating(false)} />
      <UnregisteredTenantsDialog opened={listing} onClose={() => setListing(false)} />
    </>
  );
}

import { Alert, Button, Group, Loader, Paper, Stack, Text, Title } from "@mantine/core";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ApiError } from "../../api/http";
import type { McpApproval, McpRequestInfo } from "../../api/types";
import { useSessionContext } from "../../session/SessionProvider";
import classes from "../connect/ConnectView.module.css";
import { ChangePasswordForm, SignInForm } from "../connect/ConnectView";

/** Where graphos sends the browser when an MCP client asks to sign a person in. */
export const AUTHORIZE_PATH = "/authorize";

/** The request graphos passed on, from the address; null when it is not one. */
export function requestFromSearch(search: string): Omit<McpApproval, "allow"> | null {
  const query = new URLSearchParams(search);
  const client_id = query.get("client_id");
  const redirect_uri = query.get("redirect_uri");
  const code_challenge = query.get("code_challenge");
  if (!client_id || !redirect_uri || !code_challenge) return null;
  const optional = (name: string) => query.get(name) ?? undefined;
  return {
    client_id,
    redirect_uri,
    code_challenge,
    explicit: optional("explicit"),
    state: optional("state"),
    scope: optional("scope"),
    resource: optional("resource"),
  };
}

interface Props {
  /** Leaves for the client's callback (tests replace it). */
  navigate?: (url: string) => void;
}

/**
 * An MCP client's sign-in through the web UI (ADR-0008): the UI's own sign-in
 * and password change, then a card that names the client and where the answer
 * goes, with Allow and Cancel. graphos checks the request again on the answer
 * and issues the code; the browser then goes back to the client.
 */
export function AuthorizeView({ navigate = (url) => window.location.assign(url) }: Props) {
  const { t } = useTranslation();
  const { session } = useSessionContext();
  const request = useMemo(() => requestFromSearch(window.location.search), []);
  return (
    <div className={classes.root}>
      <Paper radius="xl" p="xl" className={classes.card}>
        <Stack gap="lg">
          <div className={classes.mark} aria-hidden="true" />
          {!request ? (
            <Alert color="gray" variant="light">
              {t("shell.authorize.invalid")}
            </Alert>
          ) : !session ? (
            <>
              <Text size="sm">{t("shell.authorize.intro")}</Text>
              <SignInForm />
            </>
          ) : session.me.must_change ? (
            <ChangePasswordForm />
          ) : (
            <AllowCard request={request} navigate={navigate} />
          )}
        </Stack>
      </Paper>
    </div>
  );
}

function AllowCard({ request, navigate }: { request: Omit<McpApproval, "allow">; navigate: (url: string) => void }) {
  const { t } = useTranslation();
  const { session, account, signOut } = useSessionContext();
  const [info, setInfo] = useState<McpRequestInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"allow" | "deny" | null>(null);

  const explain = (e: unknown) =>
    e instanceof ApiError && e.status === 400
      ? t("shell.authorize.invalid")
      : e instanceof ApiError && e.status === 403
        ? t("shell.authorize.notPerson")
        : t("errors.unreachableTryAgain");

  useEffect(() => {
    if (!account) return;
    const { client_id, redirect_uri, code_challenge } = request;
    account.mcpRequest({ client_id, redirect_uri, code_challenge }).then(setInfo, (e: unknown) => setError(explain(e)));
  }, [account, request]);

  const answer = async (allow: boolean) => {
    if (!account) return;
    setBusy(allow ? "allow" : "deny");
    try {
      const { redirect } = await account.mcpApprove({ ...request, allow });
      navigate(redirect);
    } catch (e) {
      setError(explain(e));
      setBusy(null);
    }
  };

  if (error) {
    return (
      <Alert color="red" variant="light">
        {error}
      </Alert>
    );
  }
  if (!info || !session) return <Loader size="sm" />;
  const client = info.client_name ?? info.client_id;
  return (
    <Stack gap="md">
      <div>
        <Title order={1} size="h3">
          {t("shell.authorize.title", { client })}
        </Title>
        <Text size="sm">{t("shell.authorize.asks", { client, user: session.me.consumer })}</Text>
        <Text size="sm" c="dimmed">
          {t("shell.authorize.sendsTo", { host: info.redirect_host })}
        </Text>
      </div>
      <Group grow>
        <Button variant="default" loading={busy === "deny"} disabled={busy !== null} onClick={() => void answer(false)}>
          {t("common.actions.cancel")}
        </Button>
        <Button loading={busy === "allow"} disabled={busy !== null} onClick={() => void answer(true)}>
          {t("shell.authorize.allow")}
        </Button>
      </Group>
      <Group justify="space-between" gap="xs">
        <Text size="xs" c="dimmed">
          {t("shell.signIn.signedInAs", { consumer: session.me.consumer })}
        </Text>
        <Button variant="subtle" size="compact-xs" onClick={signOut} disabled={busy !== null}>
          {t("shell.authorize.notYou")}
        </Button>
      </Group>
    </Stack>
  );
}

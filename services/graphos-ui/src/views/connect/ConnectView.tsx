import { Button, Paper, PasswordInput, Stack, Text, Title, UnstyledButton } from "@mantine/core";
import { IconChevronRight } from "@tabler/icons-react";
import { useState } from "react";
import { ApiError } from "../../api/http";
import { useSessionContext } from "../../session/SessionProvider";
import classes from "./ConnectView.module.css";

/**
 * Sign-in: paste a graphos token, then pick a tenant when the token has
 * more than one. The token is checked with graphos before it is kept.
 */
export function ConnectView() {
  const { session, signIn, selectTenant, signOut } = useSessionContext();
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!token.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await signIn(token.trim());
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 401
          ? "graphos doesn't recognise this token."
          : "Couldn't reach graphos. Try again.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={classes.root}>
      <Paper radius="xl" p="xl" className={classes.card}>
        <Stack gap="lg">
          <div className={classes.mark} aria-hidden="true" />
          {session ? (
            <>
              <div>
                <Title order={1} size="h3">
                  Choose a tenant
                </Title>
                <Text size="sm" c="dimmed">
                  Signed in as {session.me.consumer}
                </Text>
              </div>
              {session.me.tenants.length === 0 ? (
                <Text size="sm">This token has no tenants. Ask an administrator to add one.</Text>
              ) : (
                <div className={classes.list}>
                  {session.me.tenants.map((tenant) => (
                    <UnstyledButton key={tenant} className={classes.tenant} onClick={() => selectTenant(tenant)}>
                      <Text size="sm">{tenant}</Text>
                      <IconChevronRight size={14} className="gx-muted" />
                    </UnstyledButton>
                  ))}
                </div>
              )}
              <Button variant="subtle" color="gray" size="xs" onClick={signOut}>
                Use another token
              </Button>
            </>
          ) : (
            <>
              <div>
                <Title order={1} size="h3">
                  Sign in to graphos
                </Title>
                <Text size="sm" c="dimmed">
                  Use the token your administrator gave you. It stays in this tab only.
                </Text>
              </div>
              <PasswordInput
                label="Token"
                value={token}
                error={error}
                autoFocus
                onChange={(e) => setToken(e.currentTarget.value)}
                onKeyDown={(e) => e.key === "Enter" && void submit()}
              />
              <Button loading={busy} disabled={!token.trim()} onClick={() => void submit()}>
                Continue
              </Button>
            </>
          )}
        </Stack>
      </Paper>
    </div>
  );
}

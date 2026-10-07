import { Button, Paper, PasswordInput, ScrollArea, Stack, Text, TextInput, Title, UnstyledButton } from "@mantine/core";
import { IconChevronRight, IconSearch } from "@tabler/icons-react";
import { useState } from "react";
import { ApiError } from "../../api/http";
import { useSessionContext } from "../../session/SessionProvider";
import classes from "./ConnectView.module.css";

/** Above this many tenants the list offers a search box. */
const TENANT_SEARCH_FROM = 8;

/**
 * Sign-in: paste a graphos token, then pick a tenant when the token has
 * more than one. The token is checked with graphos before it is kept.
 * When a kept token could not be checked after a reload (graphos
 * unreachable), offers to try again rather than asking for it anew.
 */
export function ConnectView() {
  const { session, signIn, selectTenant, signOut, restoreFailed, retryRestore } = useSessionContext();
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");

  const submit = async () => {
    if (!token.trim()) return;
    setBusy(true);
    try {
      await signIn(token.trim());
      setError(null);
    } catch (e) {
      // Kept until the next answer, so the button does not move under the pointer.
      setError(
        e instanceof ApiError && e.status === 401
          ? "graphos doesn't recognise this token."
          : "Couldn't reach graphos. Try again.",
      );
    } finally {
      setBusy(false);
    }
  };

  const tenants = session?.me.tenants ?? [];
  const needle = query.trim().toLowerCase();
  const shown = needle ? tenants.filter((t) => t.toLowerCase().includes(needle)) : tenants;

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
              {tenants.length === 0 ? (
                <Text size="sm">This token has no tenants. Ask an administrator to add one.</Text>
              ) : (
                <Stack gap="xs">
                  {tenants.length > TENANT_SEARCH_FROM ? (
                    <TextInput
                      size="xs"
                      placeholder={`Find among ${tenants.length} tenants`}
                      aria-label="Find a tenant"
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
                        <Text size="sm" truncate>
                          {tenant}
                        </Text>
                        <IconChevronRight size={14} className="gx-muted" />
                      </UnstyledButton>
                    ))}
                    {shown.length === 0 ? (
                      <Text size="sm" c="dimmed" className={classes.tenant}>
                        No tenant matches
                      </Text>
                    ) : null}
                  </ScrollArea.Autosize>
                </Stack>
              )}
              <Button variant="subtle" color="gray" size="xs" onClick={signOut}>
                Use another token
              </Button>
            </>
          ) : restoreFailed ? (
            <>
              <div>
                <Title order={1} size="h3">
                  Couldn&apos;t reach graphos
                </Title>
                <Text size="sm" c="dimmed">
                  Your token is kept for this tab. Check the connection, then try again.
                </Text>
              </div>
              <Button onClick={retryRestore}>Try again</Button>
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

import { Center, Loader, MantineProvider } from "@mantine/core";
import { Notifications, notifications } from "@mantine/notifications";
import { MutationCache, QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ComponentType, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { ApiError } from "./api/http";
import { AppLayout } from "./components/AppLayout";
import { ActivityProvider } from "./data/ActivityProvider";
import { WorkspaceProvider } from "./graph/WorkspaceProvider";
import { SessionProvider, useSessionContext } from "./session/SessionProvider";
import { resolveCssVariables, theme } from "./theme/mantineTheme";
import { ActivityView } from "./views/activity/ActivityView";
import { BrowseView } from "./views/browse/BrowseView";
import { SchemaView } from "./views/schema/SchemaView";
import { ConnectView } from "./views/connect/ConnectView";
import { ExploreView } from "./views/explore/ExploreView";
import { OverviewView } from "./views/overview/OverviewView";
import { PathView } from "./views/path/PathView";
import { t } from "./i18n";
import { VIEWS } from "./views/registry";

/** The component behind each view of the registry. */
const SCREENS: Record<string, ComponentType> = {
  explore: ExploreView,
  path: PathView,
  overview: OverviewView,
  activity: ActivityView,
  browse: BrowseView,
  schema: SchemaView,
};

/** Client errors (4xx) are answers, not outages: they are not retried. */
function createQueryClient(): QueryClient {
  return new QueryClient({
    // A write that fails always says so, whichever screen started it.
    mutationCache: new MutationCache({
      onSuccess: (_data, _variables, _context, mutation) => {
        // `meta.success` and `meta.errorTitle` are i18n keys (see data/queries).
        if (mutation.meta?.success) notifications.show({ message: t(mutation.meta.success), autoClose: 2000 });
      },
      onError: (error, _variables, _context, mutation) =>
        !mutation.meta?.silent &&
        notifications.show({
          color: "red",
          title:
            error instanceof ApiError && error.isConflict
              ? t("errors.conflict")
              : t(mutation.meta?.errorTitle ?? "errors.notSaved"),
          message: error.message,
        }),
    }),
    defaultOptions: {
      queries: {
        refetchOnWindowFocus: false,
        retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
      },
    },
  });
}

/** Providers and routes: sign-in first, then the views of the chosen tenant. */
export function App() {
  const [client] = useState(createQueryClient);
  return (
    <MantineProvider theme={theme} cssVariablesResolver={resolveCssVariables} defaultColorScheme="auto">
      <Notifications position="bottom-right" />
      <QueryClientProvider client={client}>
        <SessionProvider>
          <BrowserRouter>
            <SignOutOnRejectedToken />
            <Screens />
          </BrowserRouter>
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}

function Screens() {
  const { session, restoring } = useSessionContext();
  if (restoring) {
    return (
      <Center h="100vh">
        <Loader size="sm" />
      </Center>
    );
  }
  if (!session?.tenant) return <ConnectView />;
  return (
    <TenantScope key={session.tenant}>
      <Routes>
        <Route element={<AppLayout />}>
          {VIEWS.map((view) => {
            const Screen = SCREENS[view.id];
            return Screen ? <Route key={view.id} path={view.path} element={<Screen />} /> : null;
          })}
          <Route path="*" element={<Navigate to="/explore" replace />} />
        </Route>
      </Routes>
    </TenantScope>
  );
}

/** State that belongs to one tenant; remounted when the tenant changes. */
function TenantScope({ children }: { children: ReactNode }) {
  return (
    <ActivityProvider>
      <WorkspaceProvider>{children}</WorkspaceProvider>
    </ActivityProvider>
  );
}

/** A token graphos stops accepting (removed or rotated) ends the session. */
function SignOutOnRejectedToken() {
  const client = useQueryClient();
  const { signOut } = useSessionContext();
  useEffect(() => {
    const check = (error: unknown) => {
      if (error instanceof ApiError && error.status === 401) signOut();
    };
    const queries = client.getQueryCache().subscribe((event) => check(event.query.state.error));
    const mutations = client.getMutationCache().subscribe((event) => check(event.mutation?.state.error));
    return () => {
      queries();
      mutations();
    };
  }, [client, signOut]);
  return null;
}

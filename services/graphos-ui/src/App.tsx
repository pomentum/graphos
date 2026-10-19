import { Center, Loader, MantineProvider, useMantineColorScheme } from "@mantine/core";
import { Notifications, notifications } from "@mantine/notifications";
import { MutationCache, QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ComponentType, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { addressFor, tenantBase } from "./session/tenantPath";
import { ApiError } from "./api/http";
import { AppLayout } from "./components/AppLayout";
import { ActivityProvider } from "./data/ActivityProvider";
import { WorkspaceProvider } from "./graph/WorkspaceProvider";
import { SessionProvider, useSessionContext } from "./session/SessionProvider";
import { memoryColorSchemeManager } from "./theme/colorScheme";
import { resolveCssVariables, theme } from "./theme/mantineTheme";
import { AccessView } from "./views/access/AccessView";
import { ActivityView } from "./views/activity/ActivityView";
import { BrowseView } from "./views/browse/BrowseView";
import { SchemaView } from "./views/schema/SchemaView";
import { AUTHORIZE_PATH, AuthorizeView, requestFromSearch } from "./views/authorize/AuthorizeView";
import { ConnectView } from "./views/connect/ConnectView";
import { ExploreView } from "./views/explore/ExploreView";
import { OverviewView } from "./views/overview/OverviewView";
import { PathView } from "./views/path/PathView";
import { QueryView } from "./views/query/QueryView";
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
  access: AccessView,
  query: QueryView,
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
    <MantineProvider
      theme={theme}
      cssVariablesResolver={resolveCssVariables}
      defaultColorScheme="auto"
      colorSchemeManager={COLOR_SCHEME}
    >
      <Notifications position="bottom-right" />
      <QueryClientProvider client={client}>
        <SessionProvider>
          <SignOutOnRejectedToken />
          <PreferredColorScheme />
          <Screens />
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}

function Screens() {
  const { session, checking, returnTo } = useSessionContext();
  // Finding out whether someone is signed in at the identity provider.
  if (checking) {
    return (
      <Center h="100vh">
        <Loader size="sm" />
      </Center>
    );
  }
  // An MCP client asks to sign the person in (ADR-0008): outside any tenant.
  // Only with a request in the address: a tenant named "authorize" keeps its page.
  if (window.location.pathname === AUTHORIZE_PATH && requestFromSearch(window.location.search)) {
    return <AuthorizeView />;
  }
  // Signed out, a password to change first, or no tenant chosen yet.
  if (!session?.tenant || session.me.must_change) return <ConnectView />;
  return (
    <TenantScope key={session.tenant} tenant={session.tenant} tenants={session.me.tenants}>
      {returnTo ? (
        <ReturnAfterSignIn to={returnTo} />
      ) : (
        <Routes>
          <Route element={<AppLayout />}>
            {VIEWS.map((view) => {
              const Screen = SCREENS[view.id];
              return Screen ? <Route key={view.id} path={view.path} element={<Screen />} /> : null;
            })}
            <Route path="*" element={<Navigate to="/explore" replace />} />
          </Route>
        </Routes>
      )}
    </TenantScope>
  );
}

/**
 * After signing in through the provider: the page the person was on, once.
 * Cleared only when the router is there: the router moves in a transition,
 * so clearing earlier would let the routes see the callback address first.
 */
function ReturnAfterSignIn({ to }: { to: string }) {
  const { clearReturnTo } = useSessionContext();
  const location = useLocation();
  const arrived = `${location.pathname}${location.search}` === to;
  useEffect(() => {
    if (arrived) clearReturnTo();
  }, [arrived, clearReturnTo]);
  return arrived ? null : <Navigate to={to} replace />;
}

/**
 * State that belongs to one tenant; remounted when the tenant changes. The
 * tenant is the address's first segment and the router's basename, so a
 * reload or a shared link lands on the same tenant and page; the address is
 * put right once, before the router reads it (another tenant's page becomes
 * this tenant's, an older link without a tenant gets it).
 */
function TenantScope({ tenant, tenants, children }: { tenant: string; tenants: string[]; children: ReactNode }) {
  useState(() => {
    const target = addressFor(tenant, tenants, window.location.pathname, window.location.search);
    if (target !== `${window.location.pathname}${window.location.search}`) {
      window.history.replaceState(null, "", `${target}${window.location.hash}`);
    }
    return null;
  });
  return (
    <BrowserRouter basename={tenantBase(tenant)}>
      <ActivityProvider>
        <WorkspaceProvider>{children}</WorkspaceProvider>
      </ActivityProvider>
    </BrowserRouter>
  );
}

/**
 * The light/dark choice is held in page memory (nothing is kept in the
 * browser) and, once someone is signed in, follows their preference, which
 * graphos keeps for them.
 */
const COLOR_SCHEME = memoryColorSchemeManager();

/** Applies the signed-in user's theme preference. */
function PreferredColorScheme() {
  const { session } = useSessionContext();
  const { setColorScheme } = useMantineColorScheme();
  const theme = session?.me.preferences?.theme;
  useEffect(() => {
    if (theme) setColorScheme(theme);
  }, [theme, setColorScheme]);
  return null;
}

/** A session graphos stops accepting (ended, revoked) signs out. */
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

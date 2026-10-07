import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { fetchMe, GraphosApi } from "../api/graphosApi";
import { ApiError, HttpClient } from "../api/http";
import type { Me } from "../api/types";
import { SessionStore } from "./sessionStore";

/** Where the API is reached: nginx (and the dev server) forward `/api` to graphos. */
export const API_BASE = "/api";

/** The signed-in state every screen reads. */
export interface Session {
  me: Me;
  token: string;
  /** The selected tenant, or null before one is chosen. */
  tenant: string | null;
  /** The API of the selected tenant; null before one is chosen. */
  api: GraphosApi | null;
  canWrite: boolean;
  isAdmin: boolean;
}

interface SessionContextValue {
  session: Session | null;
  /** True while a token kept from earlier in the tab is being checked. */
  restoring: boolean;
  /** The kept token could not be checked (graphos unreachable): it is kept for `retryRestore`. */
  restoreFailed: boolean;
  retryRestore: () => void;
  /** Checks the token with graphos and keeps it for the tab. */
  signIn: (token: string) => Promise<void>;
  selectTenant: (tenant: string) => void;
  signOut: () => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

/**
 * Owns the session: signs in with a token (`GET /v1/me`), remembers the
 * chosen tenant, and builds the tenant's `GraphosApi` for everything below.
 */
export function SessionProvider({
  children,
  store: storeProp,
  initial = null,
}: {
  children: ReactNode;
  store?: SessionStore;
  initial?: { me: Me; token: string; tenant: string | null } | null;
}) {
  const [store] = useState(() => storeProp ?? new SessionStore());
  const [state, setState] = useState(initial);
  const [restoring, setRestoring] = useState(() => !initial && store.load() !== null);
  const [restoreFailed, setRestoreFailed] = useState(false);

  const signIn = useCallback(
    async (token: string) => {
      const me = await fetchMe(new HttpClient(API_BASE, token));
      const remembered = store.load();
      const tenant = pickTenant(me, remembered?.token === token ? remembered.tenant : null);
      store.save({ token, tenant });
      setState({ me, token, tenant });
    },
    [store],
  );

  // After a reload: sign in again with the token kept for this tab. Only a
  // refused token is forgotten; when graphos cannot be reached it is kept,
  // so a network blip does not sign the user out.
  useEffect(() => {
    if (!restoring) return;
    const kept = store.load();
    if (!kept) {
      setRestoring(false);
      return;
    }
    signIn(kept.token)
      .then(() => setRestoreFailed(false))
      .catch((error: unknown) => {
        if (error instanceof ApiError && error.status === 401) store.clear();
        else setRestoreFailed(true);
      })
      .finally(() => setRestoring(false));
  }, [restoring, signIn, store]);

  const retryRestore = useCallback(() => setRestoring(true), []);

  const selectTenant = useCallback(
    (tenant: string) => {
      setState((current) => {
        if (!current) return current;
        store.save({ token: current.token, tenant });
        return { ...current, tenant };
      });
    },
    [store],
  );

  const signOut = useCallback(() => {
    store.clear();
    setState(null);
    setRestoreFailed(false);
  }, [store]);

  const session = useMemo<Session | null>(() => {
    if (!state) return null;
    const api = state.tenant ? new GraphosApi(new HttpClient(API_BASE, state.token), state.tenant) : null;
    return {
      ...state,
      api,
      canWrite: state.me.access === "write",
      isAdmin: state.me.admin,
    };
  }, [state]);

  const value = useMemo(
    () => ({ session, restoring, restoreFailed, retryRestore, signIn, selectTenant, signOut }),
    [session, restoring, restoreFailed, retryRestore, signIn, selectTenant, signOut],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

/** The session and its actions; throws outside `SessionProvider`. */
export function useSessionContext(): SessionContextValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSessionContext must be used inside SessionProvider");
  return value;
}

/** The signed-in session with a tenant selected; for screens behind sign-in. */
export function useTenantSession(): Session & { api: GraphosApi; tenant: string } {
  const { session } = useSessionContext();
  if (!session?.api || !session.tenant) throw new Error("no tenant selected");
  return session as Session & { api: GraphosApi; tenant: string };
}

/** The remembered tenant if the token still has it, else the only one, else none. */
export function pickTenant(me: Me, remembered: string | null): string | null {
  if (remembered && me.tenants.includes(remembered)) return remembered;
  return me.tenants.length === 1 ? (me.tenants[0] ?? null) : null;
}

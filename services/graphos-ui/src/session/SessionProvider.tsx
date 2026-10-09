import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { fetchMe, GraphosApi } from "../api/graphosApi";
import { HttpClient } from "../api/http";
import type { AuthConfig, Level, Me, OidcSignedIn, TokenResponse } from "../api/types";
import { Credentials } from "./credentials";
import { atLeast } from "./levels";

export { atLeast };

/** Where the API is reached: nginx (and the dev server) forward `/api` to graphos. */
export const API_BASE = "/api";
/** Where the identity provider sends the browser back to. */
export const CALLBACK_PATH = "/auth/callback";

/** The signed-in state every screen reads. */
export interface Session {
  me: Me;
  /** The selected tenant, or null before one is chosen. */
  tenant: string | null;
  /** The API of the selected tenant; null before one is chosen. */
  api: GraphosApi | null;
  /** The level on the selected tenant (`owner` for an admin). */
  level: Level | null;
  /** May write data on the selected tenant. */
  canWrite: boolean;
  /** May change its schema and run recompute / metrics. */
  canMaintain: boolean;
  /** May manage who has access to it. */
  isOwner: boolean;
  isAdmin: boolean;
}

/** Why the sign-in page is shown with a note. */
export type SignInNotice = "expired" | "ssoFailed" | null;

interface SessionContextValue {
  session: Session | null;
  /** How to sign in (null while it is being asked). */
  config: AuthConfig | null;
  /** True while the page finds out whether someone is already signed in (SSO). */
  checking: boolean;
  notice: SignInNotice;
  /** Signs in with a user name and password; the tokens stay in memory. */
  signIn: (user: string, password: string) => Promise<void>;
  /** Goes to the identity provider to sign in. */
  signInWithSso: () => Promise<void>;
  /** Changes the password (required first when `me.must_change`). */
  changePassword: (current: string, next: string) => Promise<void>;
  selectTenant: (tenant: string) => void;
  signOut: () => void;
  /** The page to show after signing in through the provider, once. */
  returnTo: string | null;
  clearReturnTo: () => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

interface State {
  me: Me;
  credentials: Credentials;
  tenant: string | null;
}

/**
 * Owns the session. Signs in with a user name and password
 * (`POST /v1/auth/login`) or through the identity provider; keeps the access
 * token in page memory and the session in graphos's HttpOnly cookie (no
 * script can read it; nothing in browser storage), renews the token while
 * the page is open, and builds the selected tenant's `GraphosApi`.
 *
 * Every page load first carries on the session of that cookie, so a reload
 * or a new tab stays signed in. Without one, and with single sign-on, it
 * tries a silent sign-in (`prompt=none`): someone already signed in at the
 * provider comes straight back signed in, and only someone who is not sees
 * the sign-in page. graphos holds the PKCE verifier and exchanges the code.
 */
export function SessionProvider({
  children,
  initial = null,
  fetchImpl,
  navigateTo = (url) => window.location.assign(url),
}: {
  children: ReactNode;
  /** A signed-in state to start from (tests). */
  initial?: { me: Me; tokens: TokenResponse; tenant: string | null } | null;
  fetchImpl?: typeof fetch;
  /** Leaves the page (for the identity provider); replaced in tests. */
  navigateTo?: (url: string) => void;
}) {
  const [notice, setNotice] = useState<SignInNotice>(null);
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [checking, setChecking] = useState(initial === null);
  const [returnTo, setReturnTo] = useState<string | null>(null);
  const endedRef = useRef<() => void>(() => undefined);
  const newCredentials = useCallback(
    (tokens: TokenResponse) => new Credentials(tokens, API_BASE, () => endedRef.current(), fetchImpl),
    [fetchImpl],
  );
  const [state, setState] = useState<State | null>(() =>
    initial ? { me: initial.me, tenant: initial.tenant, credentials: newCredentials(initial.tokens) } : null,
  );
  endedRef.current = () => {
    setState(null);
    setNotice("expired");
  };

  const anonymous = useMemo(() => new HttpClient(API_BASE, null, fetchImpl), [fetchImpl]);

  const startSso = useCallback(
    async (silent: boolean) => {
      const { authorization_url } = await anonymous.post<{ authorization_url: string }>("/v1/auth/oidc/start", {
        redirect_uri: `${window.location.origin}${CALLBACK_PATH}`,
        return_to: silent ? `${window.location.pathname}${window.location.search}` : "/",
        silent,
      });
      navigateTo(authorization_url);
    },
    [anonymous, navigateTo],
  );

  const enter = useCallback(
    async (tokens: TokenResponse) => {
      const credentials = newCredentials(tokens);
      const me = await fetchMe(new HttpClient(API_BASE, credentials, fetchImpl));
      setNotice(null);
      setState({ me, credentials, tenant: me.must_change ? null : pickTenant(me, null) });
    },
    [newCredentials, fetchImpl],
  );

  // Once per page load: how to sign in, then an SSO callback to finish or a
  // silent sign-in to try. (A ref, not state: StrictMode runs effects twice,
  // and a sign-in's state is single use.)
  const started = useRef(false);
  useEffect(() => {
    if (initial !== null || started.current) return;
    started.current = true;
    void (async () => {
      const found = await anonymous
        .get<AuthConfig>("/v1/auth/config")
        .catch((): AuthConfig => ({ password: true, oidc: null }));
      const auth: AuthConfig = { password: found.password !== false, oidc: found.oidc ?? null };
      setConfig(auth);
      const url = new URL(window.location.href);
      if (url.pathname === CALLBACK_PATH) {
        // Back from the provider: the address must not keep the code.
        window.history.replaceState(null, "", "/");
        const code = url.searchParams.get("code");
        const state = url.searchParams.get("state");
        if (code && state) {
          try {
            const signed = await anonymous.post<OidcSignedIn>("/v1/auth/oidc/callback", {
              code,
              state,
              session_cookie: true,
            });
            await enter(signed);
            setReturnTo(signed.return_to);
          } catch {
            setNotice("ssoFailed");
          }
        } else if (state) {
          // login_required after a silent attempt: the sign-in page, no retry.
          await anonymous
            .post("/v1/auth/oidc/callback", { state, error: url.searchParams.get("error") })
            .catch(() => undefined);
        }
        setChecking(false);
        return;
      }
      // A reload or a new tab: the session cookie (HttpOnly) carries on, if there is one.
      try {
        await enter(await anonymous.post<TokenResponse>("/v1/auth/refresh", { session_cookie: true }));
        setChecking(false);
        return;
      } catch {
        /* no session here: single sign-on, or the sign-in page */
      }
      if (auth.oidc) {
        try {
          await startSso(true); // leaves the page
          return;
        } catch {
          /* the provider is unreachable: the sign-in page still offers passwords */
        }
      }
      setChecking(false);
    })();
  }, [initial, anonymous, enter, startSso]);

  const signIn = useCallback(
    async (user: string, password: string) => {
      await enter(await anonymous.post<TokenResponse>("/v1/auth/login", { user, password, session_cookie: true }));
    },
    [anonymous, enter],
  );

  const signInWithSso = useCallback(() => startSso(false), [startSso]);

  const changePassword = useCallback(
    async (current: string, next: string) => {
      if (!state) return;
      const http = new HttpClient(API_BASE, state.credentials, fetchImpl);
      const tokens = await http.post<TokenResponse>("/v1/me/password", {
        current,
        new: next,
        session_cookie: true,
      });
      // Every older token stopped working: carry on with the new ones.
      state.credentials.apply(tokens);
      const me = await fetchMe(http);
      setState((s) => (s ? { ...s, me, tenant: s.tenant ?? pickTenant(me, null) } : s));
    },
    [state, fetchImpl],
  );

  const selectTenant = useCallback((tenant: string) => {
    setState((current) => (current ? { ...current, tenant } : current));
  }, []);

  const signOut = useCallback(() => {
    const credentials = state?.credentials;
    setState(null);
    setNotice(null);
    void credentials?.signOut(`${window.location.origin}/`).then((endSession) => {
      // Signed in through the provider: end the session there too.
      if (endSession) navigateTo(endSession);
    });
  }, [state, navigateTo]);

  const clearReturnTo = useCallback(() => setReturnTo(null), []);

  const session = useMemo<Session | null>(() => {
    if (!state) return null;
    const { me, tenant, credentials } = state;
    const api = tenant ? new GraphosApi(new HttpClient(API_BASE, credentials, fetchImpl), tenant) : null;
    const level = me.admin ? "owner" : tenant ? (me.levels[tenant] ?? null) : null;
    return {
      me,
      tenant,
      api,
      level,
      canWrite: atLeast(level, "write"),
      canMaintain: atLeast(level, "maintain"),
      isOwner: atLeast(level, "owner"),
      isAdmin: me.admin,
    };
  }, [state, fetchImpl]);

  const value = useMemo(
    () => ({
      session,
      config,
      checking,
      notice,
      signIn,
      signInWithSso,
      changePassword,
      selectTenant,
      signOut,
      returnTo,
      clearReturnTo,
    }),
    [
      session,
      config,
      checking,
      notice,
      signIn,
      signInWithSso,
      changePassword,
      selectTenant,
      signOut,
      returnTo,
      clearReturnTo,
    ],
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

/** The remembered tenant if the user still has it, else the only one, else none. */
export function pickTenant(me: Me, remembered: string | null): string | null {
  if (remembered && me.tenants.includes(remembered)) return remembered;
  return me.tenants.length === 1 ? (me.tenants[0] ?? null) : null;
}

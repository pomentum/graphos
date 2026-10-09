import { act, render, screen, waitFor } from "@testing-library/react";
import type { TokenResponse } from "../src/api/types";
import { Credentials } from "../src/session/credentials";
import { SessionProvider, useSessionContext } from "../src/session/SessionProvider";

const ME = {
  consumer: "ada",
  kind: "person",
  via: "password",
  admin: false,
  must_change: false,
  tenants: ["acme"],
  levels: { acme: "maintain" },
  create_tenants: [],
};

const SSO_CONFIG = {
  password: true,
  oidc: { issuer: "https://idp.test", client_id: "graphos", scopes: "openid", display_name: "Acme" },
};

const tokens = (access: string, refresh: string | null = "r1", expires = 900, session = false): TokenResponse => ({
  access_token: access,
  expires_in: expires,
  refresh_token: refresh,
  session: session || refresh !== null,
  must_change: false,
});

function json(body: unknown, status = 200): Response {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A graphos answering by path; records `METHOD path body`. */
function server(answers: Record<string, (body: unknown) => Response>) {
  const calls: string[] = [];
  const fetchImpl = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const path = new URL(String(url), "http://ui").pathname;
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    const auth = new Headers(init?.headers).get("Authorization");
    calls.push(`${init?.method ?? "GET"} ${path}${auth ? ` ${auth}` : ""}${body ? ` ${JSON.stringify(body)}` : ""}`);
    const answer = answers[path];
    return answer ? answer(body) : json({ detail: "not found" }, 404);
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

function Probe() {
  const { session, notice, checking, signIn, signOut } = useSessionContext();
  return (
    <div>
      <span data-testid="who">{session ? `${session.me.consumer}@${session.tenant}:${session.level}` : "none"}</span>
      <span data-testid="maintain">{String(session?.canMaintain ?? false)}</span>
      <span data-testid="notice">{notice ?? ""}</span>
      <span data-testid="checking">{String(checking)}</span>
      <button onClick={() => void signIn("ada", "pw-pw-pw-pw-1")}>in</button>
      <button onClick={signOut}>out</button>
    </div>
  );
}

afterEach(() => window.history.replaceState(null, "", "/"));

describe("signing in", () => {
  it("signs in, keeps the session in graphos's cookie (no browser storage) and opens the only tenant", async () => {
    const { calls, fetchImpl } = server({
      "/api/v1/auth/config": () => json({ password: true, oidc: null }),
      "/api/v1/auth/refresh": () => json({ detail: "no session" }, 401),
      "/api/v1/auth/login": () => json(tokens("a1", null, 900, true)),
      "/api/v1/me": () => json(ME),
    });
    const before = { local: localStorage.length, session: sessionStorage.length, cookie: document.cookie };
    render(
      <SessionProvider fetchImpl={fetchImpl}>
        <Probe />
      </SessionProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("checking").textContent).toBe("false"));
    act(() => screen.getByText("in").click());
    await waitFor(() => expect(screen.getByTestId("who").textContent).toBe("ada@acme:maintain"));
    expect(screen.getByTestId("maintain").textContent).toBe("true");
    expect(calls.slice(1, 4)).toEqual([
      'POST /api/v1/auth/refresh {"session_cookie":true}',
      'POST /api/v1/auth/login {"user":"ada","password":"pw-pw-pw-pw-1","session_cookie":true}',
      "GET /api/v1/me Bearer a1",
    ]);
    // Nothing kept by the page (the session cookie is HttpOnly: set by graphos, unreadable here).
    expect({ local: localStorage.length, session: sessionStorage.length, cookie: document.cookie }).toEqual(before);
  });
});

describe("a reload", () => {
  it("carries on the session of graphos's cookie, without signing in again", async () => {
    window.history.replaceState(null, "", "/schema");
    const navigateTo = vi.fn();
    const { calls, fetchImpl } = server({
      "/api/v1/auth/config": () => json(SSO_CONFIG),
      "/api/v1/auth/refresh": () => json(tokens("a5", null, 900, true)),
      "/api/v1/me": () => json(ME),
    });
    render(
      <SessionProvider fetchImpl={fetchImpl} navigateTo={navigateTo}>
        <Probe />
      </SessionProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("who").textContent).toBe("ada@acme:maintain"));
    expect(calls[1]).toBe('POST /api/v1/auth/refresh {"session_cookie":true}');
    expect(navigateTo).not.toHaveBeenCalled(); // no detour through the identity provider
    expect(window.location.pathname).toBe("/schema"); // on the page that was open
  });
});

describe("single sign-on", () => {
  it("tries a silent sign-in on page load", async () => {
    window.history.replaceState(null, "", "/schema?x=1");
    const navigateTo = vi.fn();
    const { calls, fetchImpl } = server({
      "/api/v1/auth/config": () => json(SSO_CONFIG),
      "/api/v1/auth/refresh": () => json({ detail: "no session" }, 401),
      "/api/v1/auth/oidc/start": () => json({ authorization_url: "https://idp.test/auth?state=s" }),
    });
    render(
      <SessionProvider fetchImpl={fetchImpl} navigateTo={navigateTo}>
        <Probe />
      </SessionProvider>,
    );
    await waitFor(() => expect(navigateTo).toHaveBeenCalledWith("https://idp.test/auth?state=s"));
    const start = JSON.parse(calls[2]!.replace("POST /api/v1/auth/oidc/start ", ""));
    expect(start).toEqual({
      redirect_uri: `${window.location.origin}/auth/callback`,
      return_to: "/schema?x=1",
      silent: true,
    });
    expect(screen.getByTestId("checking").textContent).toBe("true"); // the page is leaving
  });

  it("finishes the sign-in when the provider sends the browser back", async () => {
    window.history.replaceState(null, "", "/auth/callback?code=c1&state=s1");
    const { calls, fetchImpl } = server({
      "/api/v1/auth/config": () => json(SSO_CONFIG),
      "/api/v1/auth/oidc/callback": () => json({ ...tokens("a9"), return_to: "/schema" }),
      "/api/v1/me": () => json({ ...ME, via: "oidc" }),
    });
    render(
      <SessionProvider fetchImpl={fetchImpl} navigateTo={vi.fn()}>
        <Probe />
      </SessionProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("who").textContent).toBe("ada@acme:maintain"));
    expect(calls[1]).toBe('POST /api/v1/auth/oidc/callback {"code":"c1","state":"s1","session_cookie":true}');
    expect(window.location.pathname + window.location.search).toBe("/"); // the code is not kept
  });

  it("shows the sign-in page when the provider needs a prompt", async () => {
    window.history.replaceState(null, "", "/auth/callback?error=login_required&state=s1");
    const navigateTo = vi.fn();
    const { calls, fetchImpl } = server({
      "/api/v1/auth/config": () => json(SSO_CONFIG),
      "/api/v1/auth/oidc/callback": () => json({ detail: "login_required" }, 401),
    });
    render(
      <SessionProvider fetchImpl={fetchImpl} navigateTo={navigateTo}>
        <Probe />
      </SessionProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("checking").textContent).toBe("false"));
    expect(screen.getByTestId("who").textContent).toBe("none");
    expect(calls[1]).toBe('POST /api/v1/auth/oidc/callback {"state":"s1","error":"login_required"}');
    expect(navigateTo).not.toHaveBeenCalled(); // no loop back to the provider
  });

  it("signs out at the provider too", async () => {
    const navigateTo = vi.fn();
    const { calls, fetchImpl } = server({
      "/api/v1/auth/logout": () => json({ end_session_url: "https://idp.test/logout?client_id=graphos" }),
    });
    render(
      <SessionProvider
        fetchImpl={fetchImpl}
        navigateTo={navigateTo}
        initial={{ me: { ...ME, via: "oidc" } as never, tokens: tokens("a1", "r7"), tenant: "acme" }}
      >
        <Probe />
      </SessionProvider>,
    );
    act(() => screen.getByText("out").click());
    await waitFor(() => expect(navigateTo).toHaveBeenCalledWith("https://idp.test/logout?client_id=graphos"));
    expect(screen.getByTestId("who").textContent).toBe("none");
    expect(calls[0]).toContain('"refresh_token":"r7"');
  });
});

describe("Credentials", () => {
  it("renews before expiry with the refresh token, and ends when it cannot", async () => {
    let now = 0;
    const ended = vi.fn();
    const answers: Response[] = [json(tokens("a2", "r2")), json({ detail: "session ended" }, 401)];
    const fetchImpl = vi.fn(async () => answers.shift()!) as unknown as typeof fetch;
    const credentials = new Credentials(tokens("a1", "r1", 100), "/api", ended, fetchImpl, () => now);
    expect(await credentials.token()).toBe("a1");
    now = 81_000; // past 80% of 100 s
    expect(await credentials.token()).toBe("a2");
    const [, init] = (fetchImpl as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls[0]!;
    expect(JSON.parse(String(init.body))).toEqual({ refresh_token: "r1" });
    expect(await credentials.renew()).toBe(false); // refused: the session ended
    expect(ended).toHaveBeenCalledTimes(1);
    expect(await credentials.renew()).toBe(false);
    expect(ended).toHaveBeenCalledTimes(1);
  });

  it("renews through the session cookie when it holds no refresh token", async () => {
    let now = 0;
    const fetchImpl = vi.fn(async () => json(tokens("a2", null, 100, true))) as unknown as typeof fetch;
    const credentials = new Credentials(
      tokens("a1", null, 100, true),
      "/api",
      () => undefined,
      fetchImpl,
      () => now,
    );
    now = 81_000;
    expect(await credentials.token()).toBe("a2");
    const calls = (fetchImpl as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls;
    expect(JSON.parse(String(calls[0]![1].body))).toEqual({ session_cookie: true });
    // Signing out asks graphos to end that session and clear the cookie.
    await credentials.signOut("http://ui/");
    expect(JSON.parse(String(calls[1]![1].body))).toEqual({
      session_cookie: true,
      post_logout_redirect_uri: "http://ui/",
    });
  });

  it("shares one renewal between concurrent callers", async () => {
    const fetchImpl = vi.fn(async () => json(tokens("a2", "r2"))) as unknown as typeof fetch;
    const credentials = new Credentials(tokens("a1"), "/api", () => undefined, fetchImpl);
    await Promise.all([credentials.renew(), credentials.renew(), credentials.renew()]);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(await credentials.token()).toBe("a2");
  });
});

import { MantineProvider } from "@mantine/core";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Me } from "../src/api/types";
import { SessionProvider } from "../src/session/SessionProvider";
import { theme } from "../src/theme/mantineTheme";
import { AuthorizeView, requestFromSearch } from "../src/views/authorize/AuthorizeView";

const TOKENS = { access_token: "a1", expires_in: 900, refresh_token: null, must_change: false };
const CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";
const SEARCH =
  `?client_id=c1&redirect_uri=${encodeURIComponent("http://127.0.0.1:33418/callback")}` +
  `&explicit=1&code_challenge=${CHALLENGE}&state=s1`;

function me(must_change = false): Me {
  return {
    consumer: "rita",
    kind: "person",
    via: "password",
    admin: false,
    must_change,
    tenants: ["acme"],
    levels: { acme: "read" },
    create_tenants: [],
  };
}

interface Call {
  method: string;
  url: string;
  body: unknown;
}

function fakeGraphos(
  approve: Response = Response.json({ redirect: "http://127.0.0.1:33418/callback?code=k&state=s1" }),
) {
  const calls: Call[] = [];
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^\/api/, "");
    const method = init?.method ?? "GET";
    calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url.startsWith("/v1/auth/mcp/request")) {
      return Response.json({ client_id: "c1", client_name: "Claude Code", redirect_host: "127.0.0.1:33418" });
    }
    if (url === "/v1/auth/mcp/approve") return approve.clone();
    if (url === "/v1/auth/config") return Response.json({ password: true, oidc: null });
    if (url === "/v1/auth/refresh") return Response.json({ detail: "no session" }, { status: 401 });
    return Response.json({}, { status: 404 });
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

function renderSignedIn(fetchImpl: typeof fetch, navigate: (url: string) => void, who: Me = me()) {
  return render(
    <MantineProvider theme={theme}>
      <SessionProvider initial={{ me: who, tokens: TOKENS, tenant: null }} fetchImpl={fetchImpl}>
        <AuthorizeView navigate={navigate} />
      </SessionProvider>
    </MantineProvider>,
  );
}

afterEach(() => window.history.replaceState(null, "", "/"));

describe("AuthorizeView", () => {
  it("reads the request graphos passed on, and nothing that is not one", () => {
    expect(requestFromSearch(SEARCH)).toEqual({
      client_id: "c1",
      redirect_uri: "http://127.0.0.1:33418/callback",
      code_challenge: CHALLENGE,
      explicit: "1",
      state: "s1",
      scope: undefined,
      resource: undefined,
    });
    expect(requestFromSearch("?client_id=c1")).toBeNull();
  });

  it("names the client and where it goes, and Allow sends the browser back with the code", async () => {
    window.history.replaceState(null, "", `/authorize${SEARCH}`);
    const { calls, fetchImpl } = fakeGraphos();
    const navigate = vi.fn();
    renderSignedIn(fetchImpl, navigate);
    expect(await screen.findByText("Allow Claude Code?")).toBeInTheDocument();
    expect(screen.getByText(/as rita, with your grants/)).toBeInTheDocument();
    expect(screen.getByText(/127\.0\.0\.1:33418/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Allow" }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("http://127.0.0.1:33418/callback?code=k&state=s1"));
    const approve = calls.find((c) => c.url === "/v1/auth/mcp/approve");
    expect(approve?.body).toMatchObject({ client_id: "c1", code_challenge: CHALLENGE, state: "s1", allow: true });
  });

  it("Cancel answers access_denied through graphos", async () => {
    window.history.replaceState(null, "", `/authorize${SEARCH}`);
    const { calls, fetchImpl } = fakeGraphos(
      Response.json({ redirect: "http://127.0.0.1:33418/callback?error=access_denied&state=s1" }),
    );
    const navigate = vi.fn();
    renderSignedIn(fetchImpl, navigate);
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith(expect.stringContaining("error=access_denied")));
    expect(calls.find((c) => c.url === "/v1/auth/mcp/approve")?.body).toMatchObject({ allow: false });
  });

  it("says so when graphos does not accept the request", async () => {
    window.history.replaceState(null, "", `/authorize${SEARCH}`);
    const { fetchImpl } = fakeGraphos();
    const refused = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
      String(input).includes("/v1/auth/mcp/request")
        ? Response.json({ detail: "not a valid MCP sign-in request" }, { status: 400 })
        : fetchImpl(input, init),
    ) as unknown as typeof fetch;
    renderSignedIn(refused, vi.fn());
    expect(await screen.findByText(/This sign-in link is not valid/)).toBeInTheDocument();
  });

  it("says graphos does not sign MCP clients in when it answers 404", async () => {
    window.history.replaceState(null, "", `/authorize${SEARCH}`);
    const { fetchImpl } = fakeGraphos();
    const off = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
      String(input).includes("/v1/auth/mcp/request")
        ? Response.json({ detail: "graphos does not sign MCP clients in on this server" }, { status: 404 })
        : fetchImpl(input, init),
    ) as unknown as typeof fetch;
    renderSignedIn(off, vi.fn());
    expect(await screen.findByText(/does not sign MCP clients in on this server/)).toBeInTheDocument();
    expect(screen.queryByText(/Couldn't reach graphos/)).not.toBeInTheDocument();
  });

  it("with single sign-on, sends the person back to their MCP client instead of signing in", async () => {
    window.history.replaceState(null, "", `/authorize${SEARCH}`);
    const { fetchImpl } = fakeGraphos();
    const sso = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
      String(input).endsWith("/v1/auth/config")
        ? Response.json({
            password: true,
            oidc: {
              issuer: "https://idp.example.test",
              client_id: "graphos",
              scopes: "openid",
              display_name: "Acme SSO",
            },
          })
        : fetchImpl(input, init),
    ) as unknown as typeof fetch;
    render(
      <MantineProvider theme={theme}>
        <SessionProvider fetchImpl={sso}>
          <AuthorizeView navigate={vi.fn()} />
        </SessionProvider>
      </MantineProvider>,
    );
    expect(await screen.findByText(/signs you in at Acme SSO, not here/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in" })).not.toBeInTheDocument();
  });

  it("asks for a new password first when it must change", async () => {
    window.history.replaceState(null, "", `/authorize${SEARCH}`);
    const { fetchImpl } = fakeGraphos();
    renderSignedIn(fetchImpl, vi.fn(), me(true));
    expect(await screen.findByRole("button", { name: /change password/i })).toBeInTheDocument();
  });

  it("shows the sign-in when nobody is signed in", async () => {
    window.history.replaceState(null, "", `/authorize${SEARCH}`);
    const { fetchImpl } = fakeGraphos();
    render(
      <MantineProvider theme={theme}>
        <SessionProvider fetchImpl={fetchImpl}>
          <AuthorizeView navigate={vi.fn()} />
        </SessionProvider>
      </MantineProvider>,
    );
    expect(await screen.findByText(/An MCP client asks to use graphos as you/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });
});

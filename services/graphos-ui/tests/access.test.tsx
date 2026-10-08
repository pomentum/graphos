import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { Me } from "../src/api/types";
import { SessionProvider } from "../src/session/SessionProvider";
import { theme } from "../src/theme/mantineTheme";
import { AccessView } from "../src/views/access/AccessView";

const TOKENS = { access_token: "a1", expires_in: 900, refresh_token: "r1", must_change: false };

function me(extra: Partial<Me> = {}): Me {
  return {
    consumer: "olive",
    kind: "person",
    via: "password",
    admin: false,
    must_change: false,
    tenants: ["acme"],
    levels: { acme: "owner" },
    create_tenants: [],
    ...extra,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const OIDC = { issuer: "https://login.example.com", client_id: "graphos", scopes: "openid", display_name: "SSO" };

/** A graphos answering the Access page; records what was sent. */
function server({ sso = false }: { sso?: boolean } = {}) {
  const sent: { method: string; path: string; body: unknown }[] = [];
  const grants = [
    { subject: "user:olive", tenant: "acme", level: "owner", granted_by: "creator" },
    ...(sso ? [{ subject: "oidc-group:Graph Analysts", tenant: "acme", level: "read", granted_by: "olive" }] : []),
  ];
  const fetchImpl = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input), "http://ui");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    sent.push({ method, path: url.pathname + url.search, body });
    if (url.pathname === "/api/v1/auth/config") return json({ password: true, oidc: sso ? OIDC : null });
    if (url.pathname === "/api/v1/grants" && method === "GET") return json({ grants });
    if (url.pathname === "/api/v1/grants" && method === "POST") return json({ ...body, granted_by: "olive" }, 201);
    if (url.pathname === "/api/v1/users" && method === "GET") return json({ users: [] });
    if (url.pathname === "/api/v1/users" && method === "POST") {
      return json(
        {
          user: { name: body.name, kind: body.kind, disabled: false, must_change: false, created_at: "" },
          password: "shown-once-secret",
        },
        201,
      );
    }
    return json({ detail: "unexpected" }, 404);
  }) as unknown as typeof fetch;
  return { fetchImpl, sent };
}

function renderAccess(who: Me, fetchImpl: typeof fetch) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider theme={theme}>
      <QueryClientProvider client={client}>
        <SessionProvider initial={{ me: who, tokens: TOKENS, tenant: "acme" }} fetchImpl={fetchImpl}>
          <AccessView />
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

describe("Access", () => {
  it("lets an owner give access to its tenant, and shows no admin sections", async () => {
    const { fetchImpl, sent } = server();
    renderAccess(me(), fetchImpl);
    expect(await screen.findByText("Access to acme")).toBeInTheDocument();
    expect(screen.queryByText("Users and service accounts")).toBeNull();
    expect(screen.queryByText("All grants")).toBeNull();
    fireEvent.change(screen.getByPlaceholderText("user name"), { target: { value: "Pat" } });
    fireEvent.click(screen.getByRole("button", { name: "Give access" }));
    await waitFor(() =>
      expect(sent.find((s) => s.method === "POST")?.body).toEqual({
        subject: "user:pat",
        tenant: "acme",
        level: "read",
      }),
    );
    // Only this tenant's grants are asked for, never the users list.
    expect(sent.some((s) => s.path.startsWith("/api/v1/users"))).toBe(false);
    expect(sent.some((s) => s.method === "GET" && s.path === "/api/v1/grants?tenant=acme")).toBe(true);
    // Without single sign-on there is nothing but graphos users to choose.
    expect(screen.queryByRole("textbox", { name: "Kind" })).toBeNull();
  });

  it("with single sign-on, gives access to a provider group, its name kept as sent", async () => {
    const { fetchImpl, sent } = server({ sso: true });
    renderAccess(me(), fetchImpl);
    // A provider grant is shown by its name, tagged with what it is.
    const row = (await screen.findByText("Graph Analysts")).closest("tr") as HTMLElement;
    expect(within(row).getByText("SSO group")).toBeInTheDocument();
    // The kind list (rendered hidden until opened; jsdom cannot scroll it into view).
    expect(screen.getByRole("textbox", { name: "Kind" })).toHaveValue("graphos user");
    fireEvent.click(screen.getByRole("option", { name: "SSO role", hidden: true }));
    fireEvent.change(screen.getByPlaceholderText("role, as the provider sends it"), {
      target: { value: " Graph-Admin " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Give access" }));
    await waitFor(() =>
      expect(sent.find((s) => s.method === "POST")?.body).toEqual({
        subject: "oidc-role:Graph-Admin",
        tenant: "acme",
        level: "read",
      }),
    );
  });

  it("lets an admin create a service account and shows its secret once", async () => {
    const { fetchImpl, sent } = server();
    renderAccess(me({ consumer: "admin", admin: true }), fetchImpl);
    fireEvent.click(await screen.findByRole("button", { name: "Add user" }));
    fireEvent.click(await screen.findByText("Service account"));
    fireEvent.change(screen.getByRole("textbox", { name: /^Name/ }), { target: { value: "Loader" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("shown-once-secret")).toBeInTheDocument();
    expect(screen.getByText(/cannot show it again/)).toBeInTheDocument();
    expect(sent.find((s) => s.method === "POST" && s.path === "/api/v1/users")?.body).toEqual({
      name: "loader",
      kind: "service",
    });
  });
});

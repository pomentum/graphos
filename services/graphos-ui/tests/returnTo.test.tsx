import { render, waitFor } from "@testing-library/react";
import { App } from "../src/App";

// The views draw graphs (WebGL) that jsdom cannot run; only the routing is under test.
vi.mock("../src/views/explore/ExploreView", () => ({ ExploreView: () => <p>explore view</p> }));
vi.mock("../src/views/schema/SchemaView", () => ({ SchemaView: () => <p>schema view</p> }));
vi.mock("../src/views/path/PathView", () => ({ PathView: () => null }));
vi.mock("../src/views/overview/OverviewView", () => ({ OverviewView: () => <p>overview view</p> }));
vi.mock("../src/views/browse/BrowseView", () => ({ BrowseView: () => null }));
vi.mock("../src/components/AppLayout", async () => {
  const { Outlet } = await import("react-router-dom");
  return { AppLayout: () => <Outlet /> };
});

const ME = {
  consumer: "ada",
  kind: "person",
  via: "oidc",
  admin: false,
  must_change: false,
  tenants: ["acme"],
  levels: { acme: "read" },
  create_tenants: [],
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("after signing in through the provider", () => {
  it("opens the page the person was on", async () => {
    window.history.replaceState(null, "", "/auth/callback?code=c1&state=s1");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const path = new URL(String(input), "http://ui").pathname;
        if (path === "/api/v1/auth/config")
          return json({ password: true, oidc: { issuer: "i", client_id: "c", scopes: "s", display_name: "SSO" } });
        if (path === "/api/v1/auth/oidc/callback")
          return json({
            access_token: "a",
            expires_in: 900,
            refresh_token: "r",
            must_change: false,
            return_to: "/acme/schema",
          });
        if (path === "/api/v1/me") return json(ME);
        if (path === "/api/v1/acme/schema")
          return json({ name: "acme", vertex_labels: [], edge_labels: [], search_indexes: {} });
        return json({ detail: "not here" }, 404);
      }),
    );
    try {
      render(<App />);
      await waitFor(() => expect(window.location.pathname).toBe("/acme/schema"), { timeout: 3000 });
      // And it stays there (no later redirect to the default view).
      await new Promise((resolve) => setTimeout(resolve, 300));
      expect(window.location.pathname).toBe("/acme/schema");
      expect(document.body.textContent).toContain("schema view");
    } finally {
      vi.unstubAllGlobals();
      window.history.replaceState(null, "", "/");
    }
  });
});

describe("a reload", () => {
  it("lands on the same tenant and page, with several tenants to choose from", async () => {
    window.history.replaceState(null, "", "/demo/overview");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const path = new URL(String(input), "http://ui").pathname;
        if (path === "/api/v1/auth/config") return json({ password: true, oidc: null });
        if (path === "/api/v1/auth/refresh")
          return json({ access_token: "a", expires_in: 900, refresh_token: null, session: true, must_change: false });
        if (path === "/api/v1/me")
          return json({ ...ME, via: "password", tenants: ["acme", "demo"], levels: { acme: "read", demo: "read" } });
        return json({ detail: "not here" }, 404);
      }),
    );
    try {
      render(<App />);
      await waitFor(() => expect(document.body.textContent).toContain("overview view"), { timeout: 3000 });
      expect(window.location.pathname).toBe("/demo/overview");
      expect(document.body.textContent).not.toContain("Choose a tenant");
    } finally {
      vi.unstubAllGlobals();
      window.history.replaceState(null, "", "/");
    }
  });
});

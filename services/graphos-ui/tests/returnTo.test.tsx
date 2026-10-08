import { render, waitFor } from "@testing-library/react";
import { App } from "../src/App";

// The views draw graphs (WebGL) that jsdom cannot run; only the routing is under test.
vi.mock("../src/views/explore/ExploreView", () => ({ ExploreView: () => <p>explore view</p> }));
vi.mock("../src/views/schema/SchemaView", () => ({ SchemaView: () => <p>schema view</p> }));
vi.mock("../src/views/path/PathView", () => ({ PathView: () => null }));
vi.mock("../src/views/overview/OverviewView", () => ({ OverviewView: () => null }));
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
            return_to: "/schema",
          });
        if (path === "/api/v1/me") return json(ME);
        if (path === "/api/v1/acme/schema")
          return json({ name: "acme", vertex_labels: [], edge_labels: [], search_indexes: {} });
        return json({ detail: "not here" }, 404);
      }),
    );
    try {
      render(<App />);
      await waitFor(() => expect(window.location.pathname).toBe("/schema"), { timeout: 3000 });
      // And it stays there (no later redirect to the default view).
      await new Promise((resolve) => setTimeout(resolve, 300));
      expect(window.location.pathname).toBe("/schema");
      expect(document.body.textContent).toContain("schema view");
    } finally {
      vi.unstubAllGlobals();
      window.history.replaceState(null, "", "/");
    }
  });
});

import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Me } from "../src/api/types";
import { DeleteTenantDialog, NewTenantDialog, UnregisteredTenantsDialog } from "../src/components/TenantLifecycle";
import { SessionProvider, useSessionContext } from "../src/session/SessionProvider";
import { theme } from "../src/theme/mantineTheme";

const TOKENS = { access_token: "a1", expires_in: 900, refresh_token: "r1", must_change: false };

function me(overrides: Partial<Me> = {}): Me {
  return {
    consumer: "olive",
    kind: "person",
    via: "password",
    admin: false,
    must_change: false,
    tenants: ["acme", "team_old"],
    levels: { acme: "owner", team_old: "read" },
    create_tenants: ["team_*"],
    preferences: {},
    ...overrides,
  };
}

interface Call {
  method: string;
  url: string;
  body: unknown;
}

/** A graphos that answers from `routes` ("METHOD path?query" → body), recording every call. */
function fakeGraphos(routes: Record<string, unknown>) {
  const calls: Call[] = [];
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^\/api/, "");
    const method = init?.method ?? "GET";
    calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const key = `${method} ${url}`;
    if (!(key in routes)) return new Response(JSON.stringify({ detail: `no route ${key}` }), { status: 404 });
    const answer = routes[key];
    if (answer instanceof Response) return answer;
    return new Response(JSON.stringify(answer ?? {}), { status: 200 });
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

/** Shows the selected tenant, for assertions. */
function Selected() {
  const { session } = useSessionContext();
  return <output aria-label="selected">{session?.tenant ?? "none"}</output>;
}

function ui(fetchImpl: typeof fetch, children: React.ReactNode, who: Me = me()) {
  return (
    <MantineProvider theme={theme}>
      <QueryClientProvider client={new QueryClient()}>
        <SessionProvider initial={{ me: who, tokens: TOKENS, tenant: "acme" }} fetchImpl={fetchImpl}>
          {children}
          <Selected />
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}

const DRY_RUN = { tenant: "team_new", exists: false, restores: false, mode: "merge", changes: null, warnings: [] };

describe("a new tenant", () => {
  it("checks the name against what the user may create, creates it and opens it", async () => {
    const { calls, fetchImpl } = fakeGraphos({
      "POST /v1/tenants?dry_run=true": DRY_RUN,
      "POST /v1/tenants": { tenant: "team_new" },
      "GET /v1/me": me({ tenants: ["acme", "team_new", "team_old"], levels: { team_new: "owner" } }),
    });
    render(ui(fetchImpl, <NewTenantDialog opened onClose={() => undefined} />));
    const name = screen.getByRole("textbox", { name: /^Name/ });
    fireEvent.change(name, { target: { value: "other_x" } });
    expect(screen.getByText("You may not create a tenant named other_x.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();

    fireEvent.change(name, { target: { value: "team_new" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(screen.getByLabelText("selected")).toHaveTextContent("team_new"));
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      "POST /v1/tenants?dry_run=true",
      "POST /v1/tenants",
      "GET /v1/me",
    ]);
    expect(calls[1]!.body).toEqual({ name: "team_new" });
  });

  it("refuses a name that is taken, or kept by an unregistered tenant, before writing anything", async () => {
    const { calls, fetchImpl } = fakeGraphos({
      "POST /v1/tenants?dry_run=true": { ...DRY_RUN, restores: true },
    });
    render(ui(fetchImpl, <NewTenantDialog opened onClose={() => undefined} />));
    fireEvent.change(screen.getByRole("textbox", { name: /^Name/ }), { target: { value: "team_old2" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText(/is an unregistered tenant that keeps its data/)).toBeInTheDocument();
    expect(calls).toHaveLength(1);
  });
});

describe("deleting a tenant", () => {
  it("unregisters by default, and deletes the data only once the name is typed", async () => {
    const { calls, fetchImpl } = fakeGraphos({
      "DELETE /v1/tenants/acme": {},
      "DELETE /v1/tenants/acme?drop_data=true": {},
    });
    const onDone = vi.fn();
    const view = render(
      ui(fetchImpl, <DeleteTenantDialog name="acme" registered opened onClose={() => undefined} onDone={onDone} />),
    );
    fireEvent.click(screen.getByRole("button", { name: "Unregister" }));
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual(["DELETE /v1/tenants/acme"]);
    view.unmount();

    render(
      ui(fetchImpl, <DeleteTenantDialog name="acme" registered opened onClose={() => undefined} onDone={onDone} />),
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "Also delete its data" }));
    const forGood = screen.getByRole("button", { name: "Delete for good" });
    expect(forGood).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Type acme to confirm" }), { target: { value: "acm" } });
    expect(forGood).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Type acme to confirm" }), { target: { value: "acme" } });
    fireEvent.click(forGood);
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(2));
    expect(calls.at(-1)).toMatchObject({ method: "DELETE", url: "/v1/tenants/acme?drop_data=true" });
  });
});

describe("unregistered tenants", () => {
  it("lists them, and restores one, which opens", async () => {
    const { calls, fetchImpl } = fakeGraphos({
      "GET /v1/tenants?unregistered=true": {
        tenants: [],
        unregistered: [
          {
            name: "team_gone",
            kind: "unregistered",
            vertex_labels: 3,
            edge_labels: 2,
            created_at: null,
            unregistered_at: "2026-10-14T08:00:00Z",
            unregistered_by: "olive",
          },
          {
            name: "older",
            kind: "orphan",
            vertex_labels: 0,
            edge_labels: 0,
            created_at: null,
            unregistered_at: null,
            unregistered_by: null,
          },
        ],
      },
      "POST /v1/tenants/team_gone/restore": { status: "restored" },
      "GET /v1/me": me({ tenants: ["acme", "team_gone"] }),
    });
    render(ui(fetchImpl, <UnregisteredTenantsDialog opened onClose={() => undefined} />));
    expect(await screen.findByText("team_gone")).toBeInTheDocument();
    expect(screen.getByText(/3 labels, 2 relationship types · unregistered/)).toBeInTheDocument();
    expect(screen.getByText("no schema kept")).toBeInTheDocument();
    // An orphan can only be deleted: one Restore, two Delete data.
    expect(screen.getAllByRole("button", { name: "Restore" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Delete data" })).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(screen.getByLabelText("selected")).toHaveTextContent("team_gone"));
    expect(calls.some((c) => c.method === "POST" && c.url === "/v1/tenants/team_gone/restore")).toBe(true);
  });
});

describe("preferences", () => {
  it("are kept by graphos: choosing a tenant remembers it", async () => {
    const { calls, fetchImpl } = fakeGraphos({ "PATCH /v1/me/preferences": { last_tenant: "team_old" } });
    function Choose() {
      const { selectTenant } = useSessionContext();
      return (
        <button type="button" onClick={() => selectTenant("team_old")}>
          choose
        </button>
      );
    }
    render(ui(fetchImpl, <Choose />));
    fireEvent.click(screen.getByRole("button", { name: "choose" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({ method: "PATCH", url: "/v1/me/preferences", body: { last_tenant: "team_old" } });
    expect(screen.getByLabelText("selected")).toHaveTextContent("team_old");
  });
});

import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Me, TableStat } from "../src/api/types";
import { SessionProvider } from "../src/session/SessionProvider";
import { theme } from "../src/theme/mantineTheme";
import { DatabaseView, deadShare, lastVacuum } from "../src/views/database/DatabaseView";
import { visibleViews } from "../src/views/registry";

const TOKENS = { access_token: "a1", expires_in: 900, refresh_token: "r1", must_change: false };
const ADMIN: Me = {
  consumer: "root",
  kind: "person",
  via: "password",
  admin: true,
  must_change: false,
  tenants: ["acme"],
  levels: { acme: "owner" },
  create_tenants: [],
  preferences: {},
};

const table = (name: string, live: number, dead: number, bytes: number, extra: Partial<TableStat> = {}): TableStat => ({
  schema_name: "acme",
  table_name: name,
  n_live_tuples: live,
  n_dead_tuples: dead,
  total_bytes: bytes,
  index_bytes: 0,
  last_vacuum: null,
  last_autovacuum: null,
  last_analyze: null,
  ...extra,
});

function fakeGraphos() {
  const sent: { method: string; url: string; body: unknown }[] = [];
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^\/api/, "");
    const method = init?.method ?? "GET";
    sent.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : null });
    if (url === "/v1/db/stats")
      return Response.json({
        postgres_version: "18.1",
        database_size_bytes: 2_000_000,
        age_version: "1.8.0",
        tables: [table("Person", 100, 50, 4096), table("Team", 10, 0, 8192)],
      });
    if (url.startsWith("/v1/db/slow-queries"))
      return Response.json({
        available: true,
        note: null,
        queries: [{ query: "SELECT 1", calls: 3, total_exec_ms: 900, mean_exec_ms: 300, rows: 3 }],
      });
    if (url === "/v1/db/vacuum") return Response.json({ tables_processed: [], elapsed_ms: 5 });
    return new Response("{}", { status: 404 });
  }) as unknown as typeof fetch;
  return { sent, fetchImpl };
}

function ui(fetchImpl: typeof fetch) {
  return (
    <MantineProvider theme={theme}>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <SessionProvider initial={{ me: ADMIN, tokens: TOKENS, tenant: "acme" }} fetchImpl={fetchImpl}>
          <DatabaseView />
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}

describe("the Database page", () => {
  it("is for admins only", () => {
    const ids = (session: { isAdmin: boolean; isOwner: boolean }) => visibleViews(session).map((v) => v.id);
    expect(ids({ isAdmin: true, isOwner: true })).toContain("database");
    expect(ids({ isAdmin: false, isOwner: true })).not.toContain("database");
  });

  it("works out the last vacuum and the dead share", () => {
    expect(
      lastVacuum(table("A", 1, 0, 0, { last_vacuum: "2026-01-02T00:00:00Z", last_autovacuum: "2026-03-01T00:00:00Z" })),
    ).toBe("2026-03-01T00:00:00Z");
    expect(lastVacuum(table("A", 1, 0, 0))).toBeNull();
    expect(deadShare(table("A", 100, 50, 0))).toBeCloseTo(1 / 3);
    expect(deadShare(table("A", 0, 0, 0))).toBe(0);
  });

  it("lists the tables, biggest first, flags dead rows, and vacuums one at once", async () => {
    const { sent, fetchImpl } = fakeGraphos();
    render(ui(fetchImpl));
    expect(await screen.findByText(/^PostgreSQL 18\.1 · AGE 1\.8\.0 · .+ on disk$/)).toBeInTheDocument();
    const rows = screen.getAllByRole("row").map((r) => r.textContent ?? "");
    expect(rows.findIndex((r) => r.includes("acme.Team"))).toBeLessThan(
      rows.findIndex((r) => r.includes("acme.Person")),
    );
    expect(screen.getByText("33%")).toBeInTheDocument();
    expect(await screen.findByText("SELECT 1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Actions for acme.Person" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Vacuum" }));
    await waitFor(() => expect(sent.some((r) => r.url === "/v1/db/vacuum")).toBe(true));
    expect(sent.find((r) => r.url === "/v1/db/vacuum")!.body).toEqual({ tables: ["acme.Person"], analyze: true });
  });

  it("asks before VACUUM FULL, which locks the table", async () => {
    const { sent, fetchImpl } = fakeGraphos();
    render(ui(fetchImpl));
    fireEvent.click(await screen.findByRole("button", { name: "Actions for acme.Person" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Vacuum FULL…" }));
    const dialog = await screen.findByRole("dialog", { name: "VACUUM FULL acme.Person?" });
    expect(sent.some((r) => r.url === "/v1/db/vacuum")).toBe(false);
    fireEvent.click(within(dialog).getByRole("button", { name: "Vacuum FULL…" }));
    await waitFor(() =>
      expect(sent.find((r) => r.url === "/v1/db/vacuum")?.body).toEqual({
        tables: ["acme.Person"],
        analyze: true,
        full: true,
      }),
    );
  });
});

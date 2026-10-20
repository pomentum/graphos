import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { GraphMap, Me, TenantSchema } from "../src/api/types";
import { WorkspaceProvider } from "../src/graph/WorkspaceProvider";
import { SessionProvider } from "../src/session/SessionProvider";
import { theme } from "../src/theme/mantineTheme";
import { DiagramPanel } from "../src/views/schema/diagram/DiagramPanel";
import { buildDiagram, type Selection } from "../src/views/schema/diagram/diagramModel";

const TOKENS = { access_token: "a1", expires_in: 900, refresh_token: "r1", must_change: false };

function me(level: "owner" | "read"): Me {
  return {
    consumer: "olive",
    kind: "person",
    via: "password",
    admin: false,
    must_change: false,
    tenants: ["acme"],
    levels: { acme: level },
    create_tenants: [],
    preferences: {},
  };
}

const SCHEMA: TenantSchema = {
  name: "acme",
  vertex_labels: ["Person", "Old"],
  edge_labels: ["KNOWS", "LIKED"],
  label_hierarchy: {},
  search_indexes: {},
  rule_edge_labels: [],
  edge_endpoints: { LIKED: [{ src: "Person", dst: "Person" }] },
  deprecated: {
    vertex: { Old: { deprecated: true, replaced_by: "Person", note: null, properties: {} } },
    edge: { LIKED: { deprecated: true, replaced_by: "KNOWS", note: null, properties: {} } },
  },
  renames: [],
};

const MAP: GraphMap = {
  labels: [
    { label: "Person", count: 3 },
    { label: "Old", count: 0 },
  ],
  links: [{ src_label: "Person", type: "KNOWS", dst_label: "Person", count: 2, estimated: false }],
  estimated: false,
};

function fakeGraphos(routes: Record<string, unknown>) {
  const calls: string[] = [];
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^\/api/, "");
    const key = `${init?.method ?? "GET"} ${url}`;
    calls.push(key);
    if (!(key in routes)) return new Response(JSON.stringify({ detail: `no route ${key}` }), { status: 404 });
    return new Response(JSON.stringify(routes[key] ?? {}), { status: 200 });
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

function ui(fetchImpl: typeof fetch, selection: NonNullable<Selection>, level: "owner" | "read" = "owner") {
  const diagram = buildDiagram(SCHEMA, MAP);
  return (
    <MantineProvider theme={theme}>
      <QueryClientProvider client={new QueryClient()}>
        <SessionProvider initial={{ me: me(level), tokens: TOKENS, tenant: "acme" }} fetchImpl={fetchImpl}>
          <MemoryRouter>
            <WorkspaceProvider>
              <DiagramPanel diagram={diagram} selection={selection} properties={new Map()} onSelect={() => undefined} />
            </WorkspaceProvider>
          </MemoryRouter>
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}

const linkOf = (type: string) => buildDiagram(SCHEMA, MAP).links.find((l) => l.types.some((x) => x.type === type))!.key;

describe("the diagram's side panel", () => {
  it("marks a deprecated label and offers to rename, deprecate and drop it when empty", async () => {
    const { calls, fetchImpl } = fakeGraphos({
      "GET /v1/acme/schema": SCHEMA,
      "DELETE /v1/tenants/acme/labels/Old": { tenant: "acme", kind: "label", name: "Old", saved_queries: [] },
    });
    render(ui(fetchImpl, { kind: "label", id: "Old" }));
    expect(await screen.findByText("This label is deprecated · use Person")).toBeInTheDocument();
    expect(screen.getByText("deprecated")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rename…" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deprecate…" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(await screen.findByRole("button", { name: "Drop Old" }));
    await waitFor(() => expect(calls).toContain("DELETE /v1/tenants/acme/labels/Old"));
  });

  it("offers no drop for a label that is not deprecated, and nothing to a reader", async () => {
    const { fetchImpl } = fakeGraphos({ "GET /v1/acme/schema": SCHEMA });
    const { unmount } = render(ui(fetchImpl, { kind: "label", id: "Person" }));
    expect(await screen.findByRole("button", { name: "Rename…" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    unmount();
    render(ui(fetchImpl, { kind: "label", id: "Old" }, "read"));
    expect(await screen.findByText("This label is deprecated · use Person")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rename…" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Deprecate…" })).toBeNull();
  });

  it("marks a deprecated relationship type on a line and offers its actions with its properties", async () => {
    const { fetchImpl } = fakeGraphos({ "GET /v1/acme/schema": SCHEMA });
    render(ui(fetchImpl, { kind: "link", id: linkOf("LIKED") }));
    expect(await screen.findByText("This relationship type is deprecated · use KNOWS")).toBeInTheDocument();
    const [liked] = screen.getAllByRole("button", { name: "Properties" });
    fireEvent.click(liked!);
    expect(await screen.findByRole("button", { name: "Rename…" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });
});

import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import type { GraphMap, Me, TenantSchema } from "../src/api/types";
import { WorkspaceProvider } from "../src/graph/WorkspaceProvider";
import { isSchemaManager, managerName } from "../src/lib/schemaManager";
import { SessionProvider } from "../src/session/SessionProvider";
import { theme } from "../src/theme/mantineTheme";
import { DiagramPanel } from "../src/views/schema/diagram/DiagramPanel";
import { buildDiagram } from "../src/views/schema/diagram/diagramModel";
import { ImportSchemaDialog } from "../src/views/schema/ImportSchemaDialog";
import { SchemaView } from "../src/views/schema/SchemaView";

// The whole Schema page renders in most tests here: slower than one component.
vi.setConfig({ testTimeout: 15_000 });

const TOKENS = { access_token: "a1", expires_in: 900, refresh_token: "r1", must_change: false };

function me(level: "owner" | "maintain", consumer = "olive", via: Me["via"] = "password"): Me {
  return {
    consumer,
    kind: consumer === "importer" ? "service" : "person",
    via,
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
  vertex_labels: ["Doc", "Old"],
  edge_labels: ["LINKS_TO"],
  label_hierarchy: {},
  search_indexes: {},
  rule_edge_labels: [],
  edge_endpoints: {},
  property_schema: { mode: "off", vertex: { Doc: { properties: { title: { type: "string" } } } }, edge: {} },
  deprecated: { vertex: { Old: { deprecated: true, replaced_by: "Doc", note: null, properties: {} } } },
  renames: [],
};

const managed = (subject: string | null): TenantSchema => ({ ...SCHEMA, schema_manager: subject });

const MAP: GraphMap = {
  labels: [
    { label: "Doc", count: 3 },
    { label: "Old", count: 0 },
  ],
  links: [{ src_label: "Doc", type: "LINKS_TO", dst_label: "Doc", count: 2, estimated: false }],
  estimated: false,
};

interface Call {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: unknown;
}

type Answer = unknown | ((call: Call) => Response);

/**
 * A graphos that answers from `routes` ("METHOD path", the query left out),
 * recording every call; the schema comes with an `ETag`.
 */
function fakeGraphos(routes: Record<string, Answer>) {
  const calls: Call[] = [];
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^\/api/, "");
    const method = init?.method ?? "GET";
    const call = {
      method,
      url,
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    calls.push(call);
    const key = `${method} ${url.split("?")[0]}`;
    const answer = routes[key];
    if (answer === undefined) return new Response(JSON.stringify({ detail: `no route ${key}` }), { status: 404 });
    if (typeof answer === "function") return (answer as (call: Call) => Response)(call);
    const headers: Record<string, string> = key === "GET /v1/acme/schema" ? { ETag: '"v7"' } : {};
    return new Response(JSON.stringify(answer), { status: 200, headers });
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

const READS = {
  "GET /v1/acme/graph/map": MAP,
  "GET /v1/acme/graph/catalogue": { by_property: { title: ["Doc"] } },
  "GET /v1/acme/graph/properties": { vertex: {}, edge: {} },
  "GET /v1/jobs": { jobs: [] },
};

function ui(fetchImpl: typeof fetch, who: Me, children: ReactNode) {
  return (
    <MantineProvider theme={theme}>
      <QueryClientProvider client={new QueryClient()}>
        <SessionProvider initial={{ me: who, tokens: TOKENS, tenant: "acme" }} fetchImpl={fetchImpl}>
          <MemoryRouter>
            <WorkspaceProvider>{children}</WorkspaceProvider>
          </MemoryRouter>
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}

/** The Schema page, with the Doc and Old rows open. */
async function schemaPage(schema: TenantSchema, who: Me) {
  const fake = fakeGraphos({ "GET /v1/acme/schema": schema, "POST /v1/tenants": { name: "acme" }, ...READS });
  render(ui(fake.fetchImpl, who, <SchemaView />));
  await screen.findByRole("button", { name: "Style of Doc" });
  fireEvent.click(screen.getByText("Old"));
  return fake;
}

describe("the schema manager's name", () => {
  it("is its subject without user:, and is matched to the viewer's own subject", () => {
    expect(managerName("user:importer")).toBe("importer");
    expect(managerName(null)).toBeNull();
    expect(isSchemaManager("user:importer", { consumer: "importer", via: "password" })).toBe(true);
    expect(isSchemaManager("user:importer", { consumer: "importer", via: "oidc" })).toBe(false);
    expect(isSchemaManager("user:importer", { consumer: "olive", via: "password" })).toBe(false);
    expect(isSchemaManager(null, { consumer: "olive", via: "password" })).toBe(false);
    // With the subject graphos gives for the viewer: single sign-on manages too.
    expect(isSchemaManager("oidc-sub:ada-1", { consumer: "Ada", via: "oidc", subject: "oidc-sub:ada-1" })).toBe(true);
    expect(isSchemaManager("user:Ada", { consumer: "Ada", via: "oidc", subject: "oidc-sub:ada-1" })).toBe(false);
    expect(isSchemaManager("user:importer", { consumer: "importer", via: "password", subject: "user:importer" })).toBe(
      true,
    );
    expect(isSchemaManager("user:importer", { consumer: "importer", via: "none", subject: null })).toBe(false);
  });
});

describe("a schema managed by another program", () => {
  it("disables the structure controls and keeps the style controls", async () => {
    await schemaPage(managed("user:importer"), me("owner"));
    expect(
      await screen.findByText(
        "The structure of this schema is managed by importer: change it in that program. Styles can still be changed here.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Enforce declared pairs" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Rename…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Deprecate…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Define a property" })).toBeDisabled();

    // The property-checks mode cannot be set; the menu says why.
    fireEvent.click(screen.getByRole("button", { name: "Property checks: off" }));
    const menu = await screen.findByRole("menu", { hidden: true });
    expect(within(menu).getByText("Managed by importer: change it in that program")).toBeInTheDocument();
    const items = within(menu).getAllByRole("menuitem", { hidden: true });
    for (const item of items.slice(0, 3)) expect(item).toBeDisabled();
    // Checking the stored rows writes nothing, and stays.
    expect(items[3]).toHaveTextContent("Check the stored rows");
    expect(items[3]).toBeEnabled();

    // Styles, export and import (into another tenant) stay.
    expect(screen.getByRole("button", { name: "Style of Doc" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Style of LINKS_TO" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Export" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();
  });

  it("says why on hover", async () => {
    await schemaPage(managed("user:importer"), me("owner"));
    fireEvent.mouseEnter(screen.getByRole("button", { name: "Add" }).parentElement!);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Managed by importer: change it in that program");
  });

  it("keeps everything editable for the manager itself, with a quiet note", async () => {
    await schemaPage(managed("user:importer"), me("owner", "importer"));
    expect(await screen.findByText("You manage this schema's structure")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Release" })).toBeNull();
    expect(screen.getByRole("button", { name: "Add" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Rename…" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Define a property" })).toBeEnabled();
  });

  it("keeps everything editable when nobody manages the schema", async () => {
    await schemaPage(managed(null), me("owner"));
    expect(screen.getByRole("button", { name: "Add" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Rename…" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Deprecate…" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Define a property" })).toBeEnabled();
    expect(screen.queryByText(/managed by/i)).toBeNull();
  });

  it("does not take a single sign-on person of the same name for the manager", async () => {
    await schemaPage(managed("user:importer"), me("owner", "importer", "oidc"));
    expect(await screen.findByRole("button", { name: "Release" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
  });
});

describe("releasing the schema", () => {
  it("lets an owner release it, after a confirmation, with the schema's If-Match", async () => {
    const { calls } = await schemaPage(managed("user:importer"), me("owner"));
    fireEvent.click(await screen.findByRole("button", { name: "Release" }));
    expect(await screen.findByText(/may overwrite what people change here/)).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Release the schema" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const release = calls.find((c) => c.method === "POST")!;
    expect(release.url).toBe("/v1/tenants");
    expect(release.body).toEqual({ name: "acme", schema_manager: null });
    expect(release.headers["If-Match"]).toBe('"v7"');
    // The schema is read again.
    await waitFor(() => expect(calls.filter((c) => c.url === "/v1/acme/schema").length).toBeGreaterThan(1));
  });

  it("offers no release to a maintainer", async () => {
    await schemaPage(managed("user:importer"), me("maintain"));
    expect(await screen.findByText(/managed by importer: change it in that program/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Release" })).toBeNull();
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
  });
});

describe("the diagram's side panel of a managed schema", () => {
  const linkOf = (schema: TenantSchema, type: string) =>
    buildDiagram(schema, MAP).links.find((l) => l.types.some((x) => x.type === type))!.key;
  const panel = (fetchImpl: typeof fetch, schema: TenantSchema, who: Me) =>
    ui(
      fetchImpl,
      who,
      <DiagramPanel
        diagram={buildDiagram(schema, MAP)}
        selection={{ kind: "link", id: linkOf(schema, "LINKS_TO") }}
        properties={new Map()}
        onSelect={() => undefined}
      />,
    );

  it("disables declaring and removing pairs", async () => {
    const declared = { ...managed("user:importer"), edge_endpoints: { LINKS_TO: [{ src: "Doc", dst: "Doc" }] } };
    const { fetchImpl } = fakeGraphos({ "GET /v1/acme/schema": declared });
    render(panel(fetchImpl, declared, me("owner")));
    await waitFor(() => expect(screen.getByRole("button", { name: "Remove" })).toBeDisabled());
  });

  it("locks once a write is refused because a program now manages the schema", async () => {
    let reads = 0;
    const { calls, fetchImpl } = fakeGraphos({
      "GET /v1/acme/schema": () =>
        new Response(JSON.stringify(reads++ === 0 ? managed(null) : managed("user:importer")), {
          status: 200,
          headers: { ETag: `"v${reads}"` },
        }),
      "POST /v1/tenants": () =>
        new Response(
          JSON.stringify({
            detail: "The schema of 'acme' is managed by user:importer",
            code: "schema_managed",
            schema_manager: "user:importer",
          }),
          { status: 409 },
        ),
    });
    render(panel(fetchImpl, managed(null), me("owner")));
    const declare = await screen.findByRole("button", { name: "Declare" });
    await waitFor(() => expect(calls.some((c) => c.url === "/v1/acme/schema")).toBe(true));
    fireEvent.click(declare);
    await waitFor(() => expect(screen.getByRole("button", { name: "Declare" })).toBeDisabled());
    expect(calls.filter((c) => c.url === "/v1/acme/schema").length).toBe(2);
  });
});

describe("importing a schema file into a managed tenant", () => {
  it("offers only another tenant", () => {
    render(
      <MantineProvider theme={theme}>
        <ImportSchemaDialog
          tenant="acme"
          locked="Managed by importer: change it in that program"
          mayCreate={() => true}
          onPreview={() => Promise.reject(new Error("unused"))}
          onImport={() => undefined}
          importing={false}
          onCancel={() => undefined}
        />
      </MantineProvider>,
    );
    expect(screen.getByRole("radio", { name: "This tenant (acme)" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Another tenant" })).toBeChecked();
    expect(screen.getByText("Managed by importer: change it in that program")).toBeInTheDocument();
  });
});

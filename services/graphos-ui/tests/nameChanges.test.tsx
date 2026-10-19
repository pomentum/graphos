import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Me, TenantSchema } from "../src/api/types";
import { SessionProvider } from "../src/session/SessionProvider";
import { theme } from "../src/theme/mantineTheme";
import { DeprecateDialog, NameActions, RenameDialog, RenamesSection } from "../src/views/schema/NameChanges";

const TOKENS = { access_token: "a1", expires_in: 900, refresh_token: "r1", must_change: false };

function me(level: "owner" | "maintain" = "owner"): Me {
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
  vertex_labels: ["Person", "Docu", "Old"],
  edge_labels: ["WROTE"],
  label_hierarchy: {},
  search_indexes: {},
  rule_edge_labels: [],
  deprecated: { vertex: { Old: { deprecated: true, replaced_by: "Docu", note: null, properties: {} } } },
  renames: [],
};

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
    return new Response(JSON.stringify(routes[key] ?? {}), { status: 200 });
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

function ui(fetchImpl: typeof fetch, children: React.ReactNode, who: Me = me(), client = new QueryClient()) {
  return (
    <MantineProvider theme={theme}>
      <QueryClientProvider client={client}>
        <SessionProvider initial={{ me: who, tokens: TOKENS, tenant: "acme" }} fetchImpl={fetchImpl}>
          {children}
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}

const PLAN = {
  tenant: "acme",
  kind: "label",
  from: "Docu",
  to: "Document",
  dry_run: true,
  spec_parts: ["display", "property_schema"],
  indexes: [["Docu_properties_gin", "Document_properties_gin"]],
  endpoints: 1,
  rows: 12,
  saved_queries: ["docs"],
};

describe("renaming", () => {
  it("previews a label rename, then renames it", async () => {
    const { calls, fetchImpl } = fakeGraphos({
      "POST /v1/tenants/acme/schema:rename?dry_run=true": PLAN,
      "POST /v1/tenants/acme/schema:rename": { ...PLAN, dry_run: false },
    });
    const onClose = vi.fn();
    render(ui(fetchImpl, <RenameDialog schema={SCHEMA} part="vertex" name="Docu" carried={[]} onClose={onClose} />));
    const rename = screen.getByRole("button", { name: "Rename" });
    fireEvent.change(screen.getByRole("textbox", { name: "New name" }), { target: { value: "Person" } });
    expect(screen.getByText("Person is already a label or relationship type")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "New name" }), { target: { value: "Document" } });
    expect(rename).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText("12 rows keep their ids and edges.")).toBeInTheDocument();
    expect(screen.getByText(/Saved queries that name Docu, not rewritten: docs/)).toBeInTheDocument();
    fireEvent.click(rename);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(calls.map((c) => [c.method, c.url, c.body])).toEqual([
      ["POST", "/v1/tenants/acme/schema:rename?dry_run=true", { kind: "label", from: "Docu", to: "Document" }],
      ["POST", "/v1/tenants/acme/schema:rename", { kind: "label", from: "Docu", to: "Document" }],
    ]);
  });

  it("renames a property on the label", async () => {
    const plan = {
      ...PLAN,
      kind: "property",
      from: "mail",
      to: "email",
      on: "Person",
      spec_parts: [],
      rows: 3,
      warnings: ["ask search for email"],
    };
    const { calls, fetchImpl } = fakeGraphos({
      "POST /v1/tenants/acme/schema:rename?dry_run=true": plan,
      "POST /v1/tenants/acme/schema:rename": { ...plan, dry_run: false, job_id: "j1" },
    });
    const onClose = vi.fn();
    render(
      ui(fetchImpl, <RenameDialog schema={SCHEMA} part="vertex" name="Person" carried={["mail"]} onClose={onClose} />),
    );
    fireEvent.click(screen.getByRole("textbox", { name: "What" }));
    fireEvent.click(await screen.findByRole("option", { name: "Its property mail" }));
    fireEvent.change(screen.getByRole("textbox", { name: "New name" }), { target: { value: "email" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText("3 rows to rewrite.")).toBeInTheDocument();
    expect(screen.getByText("ask search for email")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(calls[1]!.body).toEqual({ kind: "property", on: "Person", from: "mail", to: "email" });
  });
});

describe("deprecating", () => {
  it("marks a label deprecated with what replaces it", async () => {
    const { calls, fetchImpl } = fakeGraphos({ "POST /v1/tenants": { tenant: "acme" } });
    const onClose = vi.fn();
    render(ui(fetchImpl, <DeprecateDialog schema={SCHEMA} part="vertex" name="Docu" carried={[]} onClose={onClose} />));
    fireEvent.change(screen.getByRole("textbox", { name: "Note" }), { target: { value: "until 2027" } });
    fireEvent.click(screen.getByRole("button", { name: "Deprecate" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(calls[0]!.body).toEqual({
      name: "acme",
      deprecated: { vertex: { Docu: { deprecated: true, note: "until 2027" } } },
    });
  });

  it("takes the mark back", async () => {
    const { calls, fetchImpl } = fakeGraphos({ "POST /v1/tenants": { tenant: "acme" } });
    const onClose = vi.fn();
    render(ui(fetchImpl, <DeprecateDialog schema={SCHEMA} part="vertex" name="Old" carried={[]} onClose={onClose} />));
    fireEvent.click(screen.getByRole("button", { name: "Not deprecated" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(calls[0]!.body).toEqual({ name: "acme", deprecated: { vertex: { Old: null } } });
  });
});

describe("the actions on a label", () => {
  it("offers dropping a deprecated, empty label to an owner", async () => {
    const { calls, fetchImpl } = fakeGraphos({
      "DELETE /v1/tenants/acme/labels/Old": { tenant: "acme", kind: "label", name: "Old", saved_queries: [] },
    });
    render(ui(fetchImpl, <NameActions schema={SCHEMA} part="vertex" name="Old" rows={0} carried={[]} />));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(await screen.findByRole("button", { name: "Drop Old" }));
    await waitFor(() =>
      expect(calls.map((c) => `${c.method} ${c.url}`)).toContain("DELETE /v1/tenants/acme/labels/Old"),
    );
  });

  it("offers no drop while a label has rows or is not deprecated, and no rename to a maintainer", () => {
    const { fetchImpl } = fakeGraphos({});
    const { unmount } = render(
      ui(fetchImpl, <NameActions schema={SCHEMA} part="vertex" name="Old" rows={4} carried={[]} />),
    );
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    unmount();
    render(
      ui(fetchImpl, <NameActions schema={SCHEMA} part="vertex" name="Docu" rows={0} carried={[]} />, me("maintain")),
    );
    expect(screen.queryByRole("button", { name: "Rename…" })).toBeNull();
    expect(screen.getByRole("button", { name: "Deprecate…" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
  });
});

describe("the renames", () => {
  it("lists them newest first, a running one with its progress", async () => {
    const { fetchImpl } = fakeGraphos({
      "GET /v1/jobs/j1": {
        id: "j1",
        kind: "rename_property",
        tenant: "acme",
        status: "running",
        params: {},
        result: null,
        error: null,
        consumer: "olive",
        cancel_requested: false,
        sync: false,
        progress: { done: 50, total: 200 },
        created_at: "2026-10-19T10:00:00Z",
        started_at: "2026-10-19T10:00:01Z",
        finished_at: null,
      },
    });
    const schema: TenantSchema = {
      ...SCHEMA,
      renames: [
        { kind: "label", from: "Doc", to: "Docu", at: "2026-10-01T10:00:00Z", by: "olive", status: "done" },
        {
          kind: "property",
          from: "mail",
          to: "email",
          on: "Person",
          at: "2026-10-19T10:00:00Z",
          status: "running",
          job_id: "j1",
        },
      ],
    };
    render(ui(fetchImpl, <RenamesSection schema={schema} />));
    const rows = screen.getAllByText(/→/).map((n) => n.textContent);
    expect(rows).toEqual(["mail → email", "Doc → Docu"]);
    expect(screen.getByText("property on Person")).toBeInTheDocument();
    expect(await screen.findByRole("progressbar", { name: "Renaming mail" })).toHaveAttribute("aria-valuenow", "25");
  });

  it("reads the schema again when a running rename's job has ended", async () => {
    const { fetchImpl } = fakeGraphos({
      "GET /v1/jobs/j1": {
        id: "j1",
        kind: "rename_property",
        tenant: "acme",
        status: "succeeded",
        params: {},
        result: {},
        error: null,
        consumer: "olive",
        cancel_requested: false,
        sync: false,
        created_at: "2026-10-19T10:00:00Z",
        started_at: "2026-10-19T10:00:01Z",
        finished_at: "2026-10-19T10:00:02Z",
      },
    });
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const schema: TenantSchema = {
      ...SCHEMA,
      renames: [
        {
          kind: "property",
          from: "mail",
          to: "email",
          on: "Person",
          at: "2026-10-19T10:00:00Z",
          status: "running",
          job_id: "j1",
        },
      ],
    };
    render(ui(fetchImpl, <RenamesSection schema={schema} />, me(), client));
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["schema", "acme"] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["propertyDefinitions", "acme"] });
  });
});

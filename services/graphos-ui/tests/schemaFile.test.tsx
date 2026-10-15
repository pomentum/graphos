import { fireEvent, screen, waitFor } from "@testing-library/react";
import { GraphosApi } from "../src/api/graphosApi";
import { HttpClient } from "../src/api/http";
import type { SchemaDocument, SchemaDryRun } from "../src/api/types";
import { mayCreate, readSchemaFile } from "../src/lib/schemaFile";
import { ImportSchemaDialog } from "../src/views/schema/ImportSchemaDialog";
import { renderWithTheme } from "./render";

const DOC: SchemaDocument = {
  _export: {
    format: "graphos.schema",
    version: 1,
    graphos: "0.7.0",
    tenant: "acme",
    exported_at: "2026-10-12T09:30:00Z",
  },
  name: "acme",
  vertex_labels: ["Doc"],
  edge_endpoints: { R: [{ src: "Doc", dst: "Doc" }] },
};

describe("reading a schema file", () => {
  it("accepts an export, or a body without a header", () => {
    expect(readSchemaFile(JSON.stringify(DOC))).toEqual({ doc: DOC });
    expect(readSchemaFile('{"name": "t"}')).toEqual({ doc: { name: "t" } });
  });

  it("says why a file is not one", () => {
    expect(readSchemaFile("not json")).toEqual({ problem: { kind: "notJson" } });
    expect(readSchemaFile("[1]")).toEqual({ problem: { kind: "notSchema" } });
    expect(readSchemaFile('{"_export": {"format": "other"}, "name": "t"}')).toEqual({
      problem: { kind: "notSchema" },
    });
    expect(readSchemaFile('{"_export": {"version": 3}, "name": "t"}')).toEqual({
      problem: { kind: "newerVersion", version: 3 },
    });
  });

  it("matches create_tenants patterns", () => {
    expect(mayCreate(["sol_*"], false, "sol_42")).toBe(true);
    expect(mayCreate(["sol_*"], false, "other")).toBe(false);
    expect(mayCreate(["a.b"], false, "aXb")).toBe(false);
    expect(mayCreate([], true, "anything")).toBe(true);
  });
});

function recorder(reply: unknown = {}) {
  const calls: { method: string; url: string; body: unknown }[] = [];
  const impl = (async (url: string, init: RequestInit) => {
    calls.push({ method: init.method ?? "GET", url, body: init.body ? JSON.parse(String(init.body)) : undefined });
    return new Response(JSON.stringify(reply), { status: 200 });
  }) as unknown as typeof fetch;
  return { api: new GraphosApi(new HttpClient("/api", "t", impl), "acme"), calls };
}

describe("schema export and import calls", () => {
  it("exports, previews and imports under another name", async () => {
    const { api, calls } = recorder({ tenant: "acme", exists: true, mode: "merge", changes: null, warnings: [] });
    await api.exportSchema();
    await api.previewSchema(DOC);
    await api.importSchema(DOC, { name: "copy", replace: true });
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      "GET /api/v1/tenants/acme/spec",
      "POST /api/v1/tenants?dry_run=true",
      "POST /api/v1/tenants?force_shrink=true",
    ]);
    expect(calls[2]?.body).toMatchObject({ name: "copy", _export: DOC._export, vertex_labels: ["Doc"] });
  });

  it("sends more than 5000 label pairs over several calls", async () => {
    const { api, calls } = recorder({ tenant: "acme", exists: true, mode: "merge", changes: null, warnings: [] });
    const pairs = Array.from({ length: 10_001 }, (_, i) => ({ src: `L${i}`, dst: "Doc" }));
    const doc = { ...DOC, edge_endpoints: { R: pairs } };
    await api.importSchema(doc);
    const sizes = calls.map((c) => ((c.body as SchemaDocument).edge_endpoints?.R ?? []).length);
    expect(sizes).toEqual([5000, 5000, 1]);
    expect(Object.keys(calls[1]?.body as object).sort()).toEqual(["edge_endpoints", "name"]);
    calls.length = 0;
    const preview = await api.previewSchema(doc);
    expect(calls).toHaveLength(1);
    expect(preview.warnings[0]).toContain("5001 more declared label pairs");
  });
});

describe("the import dialog", () => {
  const preview: SchemaDryRun = {
    tenant: "acme",
    exists: true,
    mode: "merge",
    changes: { added: { vertex_labels: ["Invoice"] } },
    warnings: ["generated column Doc.kind is added to a label with about 120 nodes"],
  };

  const open = (overrides: Partial<Parameters<typeof ImportSchemaDialog>[0]> = {}) => {
    const onPreview = vi.fn(async () => preview);
    const onImport = vi.fn();
    const view = renderWithTheme(
      <ImportSchemaDialog
        tenant="acme"
        mayCreate={(name) => name.startsWith("acme_")}
        onPreview={onPreview}
        onImport={onImport}
        importing={false}
        onCancel={() => undefined}
        {...overrides}
      />,
    );
    const choose = (text: string) => {
      const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
      fireEvent.change(input, {
        target: { files: [new File([text], "acme.schema.json", { type: "application/json" })] },
      });
    };
    return { onPreview, onImport, choose, view };
  };

  it("previews, then imports into this tenant", async () => {
    const { onPreview, onImport, choose } = open();
    choose(JSON.stringify(DOC));
    expect(await screen.findByText(/Exported from acme by graphos 0.7.0/)).toBeInTheDocument();
    const apply = screen.getByRole("button", { name: "Import" });
    expect(apply).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(onPreview).toHaveBeenCalledWith(DOC, { name: "acme", replace: false }));
    expect(await screen.findByText(/added label Invoice/)).toBeInTheDocument();
    expect(screen.getByText(/added to a label with about 120 nodes/)).toBeInTheDocument();
    expect(apply).toBeEnabled();
    fireEvent.click(apply);
    expect(onImport).toHaveBeenCalledWith(DOC, { name: "acme", replace: false });
  });

  it("asks for a new preview when the mode changes, and checks another tenant's name", async () => {
    const { onPreview, choose } = open();
    choose(JSON.stringify(DOC));
    await screen.findByText(/Exported from acme/);
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByText(/added label Invoice/);
    fireEvent.click(screen.getByRole("radio", { name: /Replace/ }));
    expect(screen.queryByText(/added label Invoice/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();

    fireEvent.click(screen.getByRole("radio", { name: "Another tenant" }));
    const name = screen.getByRole("textbox", { name: "Tenant name" });
    fireEvent.change(name, { target: { value: "1bad" } });
    expect(screen.getByText("Not a valid tenant name")).toBeInTheDocument();
    fireEvent.change(name, { target: { value: "other" } });
    expect(screen.getByText("You may not create a tenant named other")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preview" })).toBeDisabled();
    fireEvent.change(name, { target: { value: "acme_copy" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(onPreview).toHaveBeenLastCalledWith(DOC, { name: "acme_copy", replace: true }));
  });

  it("refuses a file that is not a schema, or from a newer graphos", async () => {
    const { choose } = open();
    choose("{oops");
    expect(await screen.findByText("Not a JSON file")).toBeInTheDocument();
    choose('{"_export": {"format": "graphos.schema", "version": 3}, "name": "acme"}');
    expect(await screen.findByText(/newer graphos \(format version 3\)/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preview" })).toBeDisabled();
  });
});

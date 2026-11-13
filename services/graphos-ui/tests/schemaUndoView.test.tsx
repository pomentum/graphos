import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Me, TenantSchema } from "../src/api/types";
import { useAddToSchema, useSchema } from "../src/data/queries";
import { schemaUndo } from "../src/data/schemaUndo";
import { SessionProvider } from "../src/session/SessionProvider";
import { theme } from "../src/theme/mantineTheme";
import { SchemaUndo } from "../src/views/schema/SchemaUndo";

const TOKENS = { access_token: "a1", expires_in: 900, refresh_token: "r1", must_change: false };
const ME: Me = {
  consumer: "olive",
  kind: "person",
  via: "password",
  admin: false,
  must_change: false,
  tenants: ["acme"],
  levels: { acme: "owner" },
  create_tenants: [],
  preferences: {},
};
const SCHEMA: TenantSchema = {
  name: "acme",
  vertex_labels: ["Person"],
  edge_labels: [],
  label_hierarchy: {},
  search_indexes: {},
  rule_edge_labels: [],
  display: { vertex: { Person: { color: "#123456" } } },
};

/** A graphos that serves the schema with an ETag and records each write, refusing them with `refuse`. */
function fakeGraphos(refuse?: number) {
  const writes: { method: string; url: string; body: unknown; ifMatch: string | null }[] = [];
  let version = 1;
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^\/api/, "");
    const method = init?.method ?? "GET";
    if (method === "GET" && url === "/v1/acme/schema")
      return new Response(JSON.stringify(SCHEMA), { status: 200, headers: { ETag: `"v${version}"` } });
    const headers = new Headers(init?.headers);
    writes.push({
      method,
      url,
      body: init?.body ? JSON.parse(String(init.body)) : null,
      ifMatch: headers.get("If-Match"),
    });
    if (refuse && writes.length > 1)
      return new Response(JSON.stringify({ detail: "Changed by someone else" }), { status: refuse });
    version++;
    return new Response("{}", { status: 200 });
  }) as unknown as typeof fetch;
  return { writes, fetchImpl };
}

function Editor() {
  const schema = useSchema();
  const add = useAddToSchema();
  return schema.data ? (
    <>
      <button onClick={() => add.mutate({ display: { vertex: { Person: { color: "#ff0000" } } } })}>Make it red</button>
      <SchemaUndo />
    </>
  ) : null;
}

function ui(fetchImpl: typeof fetch) {
  return (
    <MantineProvider theme={theme}>
      <QueryClientProvider client={new QueryClient()}>
        <SessionProvider initial={{ me: ME, tokens: TOKENS, tenant: "acme" }} fetchImpl={fetchImpl}>
          <MemoryRouter>
            <Editor />
          </MemoryRouter>
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}

const undoButton = () => screen.getByRole("button", { name: "Undo" });

describe("Undo on the Schema page", () => {
  beforeEach(() => schemaUndo.clear("acme"));

  it("undoes a style change with Ctrl+Z, sending the style back as it was", async () => {
    const { writes, fetchImpl } = fakeGraphos();
    render(ui(fetchImpl));
    fireEvent.click(await screen.findByRole("button", { name: "Make it red" }));
    await waitFor(() => expect(undoButton()).toBeEnabled());
    fireEvent.keyDown(document.documentElement, { key: "z", code: "KeyZ", ctrlKey: true });
    await waitFor(() => expect(writes).toHaveLength(2));
    expect(writes[1]).toMatchObject({
      method: "POST",
      url: "/v1/tenants",
      body: { name: "acme", display: { vertex: { Person: { color: "#123456" } } }, display_merge: "entries" },
    });
    await waitFor(() => expect(undoButton()).toBeDisabled());
  });

  it("leaves Ctrl+Z to a text field", async () => {
    const { writes, fetchImpl } = fakeGraphos();
    render(
      <>
        {ui(fetchImpl)}
        <input aria-label="note" />
      </>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Make it red" }));
    await waitFor(() => expect(undoButton()).toBeEnabled());
    fireEvent.keyDown(screen.getByLabelText("note"), { key: "z", code: "KeyZ", ctrlKey: true });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(writes).toHaveLength(1);
  });

  it("empties the history when graphos refuses the undo (someone saved since)", async () => {
    const { writes, fetchImpl } = fakeGraphos(412);
    render(ui(fetchImpl));
    fireEvent.click(await screen.findByRole("button", { name: "Make it red" }));
    await waitFor(() => expect(undoButton()).toBeEnabled());
    fireEvent.click(undoButton());
    await waitFor(() => expect(writes).toHaveLength(2));
    await waitFor(() => expect(schemaUndo.get("acme").steps).toEqual([]));
    expect(undoButton()).toBeDisabled();
  });
});

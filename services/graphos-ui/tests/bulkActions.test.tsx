import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Me } from "../src/api/types";
import { SessionProvider } from "../src/session/SessionProvider";
import { theme } from "../src/theme/mantineTheme";
import { BulkActions, bulkValue } from "../src/views/browse/BulkActions";

const TOKENS = { access_token: "a1", expires_in: 900, refresh_token: "r1", must_change: false };
const ME: Me = {
  consumer: "olive",
  kind: "person",
  via: "password",
  admin: false,
  must_change: false,
  tenants: ["acme"],
  levels: { acme: "write" },
  create_tenants: [],
  preferences: {},
};
const ROWS = [
  { id: "p1", label: "Person", properties: { name: "Ana" } },
  { id: "p2", label: "Person", properties: { name: "Bo" } },
];

function fakeGraphos() {
  const sent: { method: string; url: string; body: unknown }[] = [];
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^\/api/, "");
    sent.push({ method: init?.method ?? "GET", url, body: init?.body ? JSON.parse(String(init.body)) : null });
    if (url.includes("nodes:delete")) return new Response(JSON.stringify({ nodes_deleted: 2 }), { status: 200 });
    return new Response(JSON.stringify({ id: "x", label: "Person", properties: {}, rev: 2 }), { status: 200 });
  }) as unknown as typeof fetch;
  return { sent, fetchImpl };
}

function ui(fetchImpl: typeof fetch, onDone = () => undefined) {
  return (
    <MantineProvider theme={theme}>
      <QueryClientProvider client={new QueryClient()}>
        <SessionProvider initial={{ me: ME, tokens: TOKENS, tenant: "acme" }} fetchImpl={fetchImpl}>
          <BulkActions rows={ROWS} names={["name", "team"]} onDone={onDone} />
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}

describe("bulkValue", () => {
  it("types the value, and refuses a number that is not one", () => {
    expect(bulkValue("text", "42")).toEqual({ value: "42" });
    expect(bulkValue("number", " 4.5 ")).toEqual({ value: 4.5 });
    expect(bulkValue("number", "four")).toEqual({ problem: "number" });
    expect(bulkValue("number", "")).toEqual({ problem: "number" });
    expect(bulkValue("true", "")).toEqual({ value: true });
    expect(bulkValue("remove", "x")).toEqual({ value: null });
  });
});

describe("Browse's bulk actions", () => {
  it("sets one property on every picked node", async () => {
    const { sent, fetchImpl } = fakeGraphos();
    const onDone = vi.fn();
    render(ui(fetchImpl, onDone));
    fireEvent.click(screen.getByRole("button", { name: "Set a property…" }));
    const dialog = await screen.findByRole("dialog", { name: "Set a property on 2 nodes" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Property" }), { target: { value: "team" } });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Value" }), { target: { value: "data" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Set" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    const patches = sent.filter((r) => r.method === "PATCH");
    expect(patches.map((r) => r.url.split("?")[0])).toEqual(["/v1/acme/graph/nodes/p1", "/v1/acme/graph/nodes/p2"]);
    expect(patches[0]!.body).toEqual({ team: "data" });
  });

  it("deletes the picked nodes only once confirmed", async () => {
    const { sent, fetchImpl } = fakeGraphos();
    render(ui(fetchImpl));
    fireEvent.click(screen.getByRole("button", { name: "Delete…" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete 2 nodes?" });
    expect(sent).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(sent.some((r) => r.url.endsWith("nodes:delete"))).toBe(true));
    expect(sent.find((r) => r.url.endsWith("nodes:delete"))!.body).toEqual({
      nodes: [
        { id: "p1", label: "Person" },
        { id: "p2", label: "Person" },
      ],
    });
  });
});

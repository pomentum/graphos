import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import type { CypherResult, Me, SavedQuery } from "../src/api/types";
import { useWorkspace, WorkspaceProvider } from "../src/graph/WorkspaceProvider";
import { SessionProvider } from "../src/session/SessionProvider";
import { theme } from "../src/theme/mantineTheme";
import { cellText, paramValue, queryParams } from "../src/views/query/cypherText";
import { QueryView } from "../src/views/query/QueryView";

const TOKENS = { access_token: "a1", expires_in: 900, refresh_token: "r1", must_change: false };
const RESULT: CypherResult = {
  columns: ["a", "w", "d"],
  rows: [
    [
      { node: { id: "a1", label: "Author", properties: { name: "Ada" } } },
      {
        edge: {
          type: "WROTE",
          properties: {},
          src_id: "a1",
          src_label: "Author",
          dst_id: "d1",
          dst_label: "Doc",
        },
      },
      { node: { id: "d1", label: "Doc", properties: {} } },
    ],
  ],
  graph: {
    nodes: [
      { id: "a1", label: "Author", properties: { name: "Ada" } },
      { id: "d1", label: "Doc", properties: {} },
    ],
    edges: [{ type: "WROTE", properties: {}, src_id: "a1", src_label: "Author", dst_id: "d1", dst_label: "Doc" }],
  },
  truncated: true,
  elapsed_ms: 12,
};
const SAVED: SavedQuery = {
  name: "docs_of",
  description: "Docs of an author",
  query: "MATCH (a:Author {id: $author})-[w:WROTE]->(d) RETURN a, w, d",
  params: { author: { type: "string", label: "Author id" } },
  show: "graph",
};

function me(level: "read" | "maintain"): Me {
  return {
    consumer: "rita",
    kind: "person",
    via: "password",
    admin: false,
    must_change: false,
    tenants: ["acme"],
    levels: { acme: level },
    create_tenants: [],
  };
}

interface Call {
  method: string;
  url: string;
  body: unknown;
}

function fakeGraphos(cypher: boolean, answer: Response | null = null) {
  const calls: Call[] = [];
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^\/api/, "");
    const method = init?.method ?? "GET";
    calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url === "/v1/info") {
      return Response.json({ service: "graphos", version: "x", features: { louvain: false, metrics: true, cypher } });
    }
    if (url === "/v1/acme/cypher/saved") return Response.json([SAVED]);
    if (answer && method === "POST") return answer.clone();
    if (method === "POST") return Response.json(RESULT);
    return Response.json({}, { status: 404 });
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

/** The canvas's node count and the page, for assertions. */
function Probe() {
  const { model } = useWorkspace();
  const location = useLocation();
  return <output aria-label="probe">{`${location.pathname}|${model.graph.order}`}</output>;
}

function ui(fetchImpl: typeof fetch, level: "read" | "maintain" = "read") {
  return (
    <MantineProvider theme={theme}>
      <QueryClientProvider client={new QueryClient()}>
        <SessionProvider initial={{ me: me(level), tokens: TOKENS, tenant: "acme" }} fetchImpl={fetchImpl}>
          <MemoryRouter initialEntries={["/query"]}>
            <WorkspaceProvider>
              <Routes>
                <Route path="/query" element={<QueryView />} />
                <Route path="/explore" element={null} />
              </Routes>
              <Probe />
            </WorkspaceProvider>
          </MemoryRouter>
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}

describe("the query text", () => {
  it("finds parameters outside strings and comments, and types their values", () => {
    expect(queryParams("MATCH (n {id: $id, k: $k}) WHERE n.x = '$no' // $nor\nRETURN n, $id")).toEqual(["id", "k"]);
    expect(paramValue("5")).toBe(5);
    expect(paramValue("true")).toBe(true);
    expect(paramValue('["a", 1]')).toEqual(["a", 1]);
    expect(paramValue("d1")).toBe("d1");
    expect(paramValue("5", "string")).toBe("5");
    expect(paramValue("x", "list")).toEqual([]);
  });

  it("writes cells as text: nodes, edges, paths, values", () => {
    expect(RESULT.rows[0]!.map(cellText)).toEqual(["Author a1", "WROTE", "Doc d1"]);
    expect(cellText(RESULT.rows[0])).toBe("Author a1 → WROTE → Doc d1");
    expect(cellText({ a: 1 })).toBe('{"a":1}');
    expect(cellText(null)).toBe("");
  });
});

describe("the Query view", () => {
  it("runs a query with its parameters and shows the table", async () => {
    const { calls, fetchImpl } = fakeGraphos(true);
    render(ui(fetchImpl));
    const editor = await screen.findByRole("textbox", { name: "Cypher query" });
    fireEvent.change(editor, { target: { value: "MATCH (d:Doc {id: $id, pages: $pages}) RETURN d" } });
    fireEvent.change(screen.getByRole("textbox", { name: "$id" }), { target: { value: "d1" } });
    fireEvent.change(screen.getByRole("textbox", { name: "$pages" }), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("Author a1")).toBeInTheDocument();
    expect(screen.getByText(/1 row in 12 ms · more rows matched/)).toBeInTheDocument();
    const run = calls.find((c) => c.url === "/v1/acme/cypher");
    expect(run?.body).toEqual({
      query: "MATCH (d:Doc {id: $id, pages: $pages}) RETURN d",
      params: { id: "d1", pages: 5 },
    });
    // Readers run saved queries; only maintainers save them.
    expect(screen.queryByRole("button", { name: "Save…" })).not.toBeInTheDocument();
  });

  it("runs a saved query from its form, and opens the result on the canvas", async () => {
    const { calls, fetchImpl } = fakeGraphos(true);
    render(ui(fetchImpl, "maintain"));
    fireEvent.click(await screen.findByText("docs_of"));
    fireEvent.change(screen.getByRole("textbox", { name: "Author id" }), { target: { value: "a1" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByText("Author a1");
    expect(calls.find((c) => c.url === "/v1/acme/cypher/saved/docs_of:run")?.body).toEqual({
      params: { author: "a1" },
    });
    expect(screen.getByRole("button", { name: "Save…" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open 2 nodes on the canvas" }));
    await waitFor(() => expect(screen.getByLabelText("probe")).toHaveTextContent("/explore|2"));
  });

  it("shows what graphos refused", async () => {
    const refused = Response.json(
      { detail: "the console runs Cypher's own functions only, not database functions: pg_catalog.version" },
      { status: 400 },
    );
    const { fetchImpl } = fakeGraphos(true, refused);
    render(ui(fetchImpl));
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));
    expect(await screen.findByText(/not database functions: pg_catalog.version/)).toBeInTheDocument();
  });

  it("says when the console is off", async () => {
    const { fetchImpl } = fakeGraphos(false);
    render(ui(fetchImpl));
    expect(await screen.findByText(/The Cypher console is off/)).toBeInTheDocument();
  });
});

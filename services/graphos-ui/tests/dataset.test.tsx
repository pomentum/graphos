import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { GraphosApi } from "../src/api/graphosApi";
import { HttpClient } from "../src/api/http";
import type { DatasetImportResult, Me } from "../src/api/types";
import { headerProblem, readDatasetHeader } from "../src/lib/datasetFile";
import { SessionProvider } from "../src/session/SessionProvider";
import { theme } from "../src/theme/mantineTheme";
import { DatasetPanel } from "../src/views/overview/DatasetPanel";

const HEADER = {
  _kind: "header",
  format: "graphos.snapshot",
  version: 1,
  tenant: "acme",
  exported_at: "2026-10-12T10:00:00Z",
  counts: { nodes: 1200, edges: 3400 },
  schema: { name: "acme", vertex_labels: ["Doc"] },
};
const LINES = [HEADER, { _kind: "node", id: "d1", label: "Doc", properties: {} }];
const FILE_TEXT = LINES.map((line) => JSON.stringify(line)).join("\n") + "\n";

describe("reading a dataset file's header", () => {
  it("reads the first line only, and refuses another format or a newer version", async () => {
    expect(await readDatasetHeader(new Blob([FILE_TEXT]))).toEqual(HEADER);
    expect(await readDatasetHeader(new Blob(['{"_kind":"node","id":"d1"}\n']))).toBeNull();
    expect(await readDatasetHeader(new Blob(["not json\n"]))).toBeNull();
    expect(headerProblem({ ...HEADER, _kind: "header", version: 2 })).toBe("newerVersion");
    expect(headerProblem({ ...HEADER, _kind: "header", format: "other" })).toBe("notDataset");
    expect(headerProblem({ ...HEADER, _kind: "header" })).toBeNull();
  });
});

describe("dataset calls", () => {
  it("exports as a stream and uploads the file as it is", async () => {
    const calls: { method: string; url: string; body: unknown; type: string | null }[] = [];
    const fetchImpl = (async (url: string, init: RequestInit) => {
      calls.push({
        method: init.method ?? "GET",
        url,
        body: init.body,
        type: new Headers(init.headers).get("Content-Type"),
      });
      return new Response(JSON.stringify({ nodes_upserted: 1 }), { status: 200 });
    }) as unknown as typeof fetch;
    const api = new GraphosApi(new HttpClient("/api", "t", fetchImpl), "acme");
    const file = new Blob([FILE_TEXT]);
    await api.exportDataset({ gzip: true });
    await api.importDataset(file, "empty");
    expect(calls[0]).toMatchObject({ method: "GET", url: "/api/v1/acme/graph/snapshot?gzip=true&computed=true" });
    expect(calls[1]).toMatchObject({
      method: "POST",
      url: "/api/v1/acme/graph/snapshot-import?mode=empty",
      type: "application/x-ndjson",
    });
    expect(calls[1]!.body).toBe(file); // not read into the page: sent as the file
  });
});

const TOKENS = { access_token: "a1", expires_in: 900, refresh_token: "r1", must_change: false };

function me(level: "read" | "write" | "maintain"): Me {
  return {
    consumer: "olive",
    kind: "person",
    via: "password",
    admin: false,
    must_change: false,
    tenants: ["acme"],
    levels: { acme: level },
    create_tenants: [],
  };
}

function renderPanel(who: Me, onImport = vi.fn()) {
  const fetchImpl = vi.fn(async () => new Response("{}", { status: 200 })) as unknown as typeof fetch;
  render(
    <MantineProvider theme={theme}>
      <QueryClientProvider client={new QueryClient()}>
        <SessionProvider initial={{ me: who, tokens: TOKENS, tenant: "acme" }} fetchImpl={fetchImpl}>
          <DatasetPanel onImport={onImport} />
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>,
  );
  return onImport;
}

function choose(text: string) {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  fireEvent.change(input, { target: { files: [new File([text], "acme.ndjson", { type: "application/x-ndjson" })] } });
}

describe("the dataset panel", () => {
  it("offers only the download to a reader", () => {
    renderPanel(me("read"));
    expect(screen.getByRole("button", { name: "Download" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Upload" })).not.toBeInTheDocument();
  });

  it("uploads with the mode chosen, the schema first only for a maintainer who asks", async () => {
    const result: DatasetImportResult = {
      tenant: "acme",
      mode: "empty",
      nodes_received: 1,
      edges_received: 0,
      nodes_upserted: 1,
      edges_upserted: 0,
      edges_skipped: 0,
      header: null,
      complete: false,
      errors: [],
      elapsed_ms: 2400,
    };
    const onImport = renderPanel(
      me("maintain"),
      vi.fn(async () => result),
    );
    choose(FILE_TEXT);
    expect(await screen.findByText(/Exported from acme, 2026-10-12T10:00:00Z: 1,200 nodes/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Restore into this empty tenant" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Register the file's schema first" }));
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(onImport).toHaveBeenCalled());
    const [file, mode, applySchema, header] = onImport.mock.calls[0]!;
    expect([(file as File).name, mode, applySchema, header]).toEqual(["acme.ndjson", "empty", true, HEADER]);
    expect(await screen.findByText("Imported 1 nodes and 0 relationships in 2 s.")).toBeInTheDocument();
    expect(screen.getByText(/it was cut off/)).toBeInTheDocument();
  });

  it("does not offer the schema to a writer, and refuses a newer file", async () => {
    renderPanel(me("write"));
    choose(FILE_TEXT);
    await screen.findByText(/Exported from acme/);
    expect(screen.queryByRole("checkbox", { name: "Register the file's schema first" })).not.toBeInTheDocument();
    choose(JSON.stringify({ ...HEADER, version: 2 }) + "\n");
    expect(await screen.findByText(/newer graphos \(format version 2\)/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload" })).toBeDisabled();
  });
});

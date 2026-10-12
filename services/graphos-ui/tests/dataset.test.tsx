import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { GraphosApi } from "../src/api/graphosApi";
import type { DatasetImportResult, Me } from "../src/api/types";
import { TransfersIndicator } from "../src/components/Transfers";
import { headerProblem, readDatasetHeader } from "../src/lib/datasetFile";
import { cancelTransfer, resetTransfers, startDownload, startUpload, type Transfer } from "../src/lib/transfers";
import { SessionProvider } from "../src/session/SessionProvider";
import { theme } from "../src/theme/mantineTheme";
import { DatasetPanel } from "../src/views/overview/DatasetPanel";

const HEADER = {
  _kind: "header",
  format: "graphos.snapshot",
  version: 1,
  tenant: "acme",
  exported_at: "2026-10-12T10:00:00Z",
  counts: { nodes: 2, edges: 1 },
  schema: { name: "acme", vertex_labels: ["Doc"] },
};
const LINES = [
  HEADER,
  { _kind: "node", id: "d1", label: "Doc", properties: {} },
  { _kind: "node", id: "d2", label: "Doc", properties: {} },
  { _kind: "edge", src_id: "d1", src_label: "Doc", dst_id: "d2", dst_label: "Doc", type: "R", properties: {} },
  { _kind: "end", counts: { nodes: 2, edges: 1 } },
];
const FILE_TEXT = LINES.map((line) => JSON.stringify(line)).join("\n") + "\n";
const RESULT: DatasetImportResult = {
  tenant: "acme",
  mode: "empty",
  nodes_received: 2,
  edges_received: 1,
  nodes_upserted: 2,
  edges_upserted: 1,
  edges_skipped: 0,
  header: null,
  complete: true,
  errors: [],
  elapsed_ms: 2400,
};

/** A stream of `text` in small chunks; `hold` keeps it open after them (to cancel it). */
function streamOf(text: string, hold = false): ReadableStream<Uint8Array> {
  const bytes = new TextEncoder().encode(text);
  let sent = 0;
  return new ReadableStream({
    pull(controller) {
      if (sent < bytes.length) {
        controller.enqueue(bytes.slice(sent, sent + 40));
        sent += 40;
      } else if (!hold) {
        controller.close();
      } else {
        return new Promise(() => undefined);
      }
    },
  });
}

/** An `XMLHttpRequest` that sends in two halves, then answers `RESULT`. */
class FakeXhr {
  static last: FakeXhr | null = null;
  upload: {
    onprogress?: (e: { loaded: number; total: number; lengthComputable: boolean }) => void;
    onload?: () => void;
  } = {};
  onload?: () => void;
  onerror?: () => void;
  onabort?: () => void;
  status = 0;
  responseText = "";
  url = "";
  headers: Record<string, string> = {};
  body: unknown = null;
  open(_method: string, url: string) {
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }
  send(body: Blob) {
    this.body = body;
    FakeXhr.last = this;
    this.upload.onprogress?.({ loaded: body.size / 2, total: body.size, lengthComputable: true });
  }
  finish() {
    this.upload.onprogress?.({
      loaded: (this.body as Blob).size,
      total: (this.body as Blob).size,
      lengthComputable: true,
    });
    this.upload.onload?.();
  }
  answer() {
    this.status = 200;
    this.responseText = JSON.stringify(RESULT);
    this.onload?.();
  }
  abort() {
    this.onabort?.();
  }
}

let latest: Transfer[] = [];
async function settled(predicate: (t: Transfer) => boolean): Promise<Transfer> {
  let found: Transfer | undefined;
  await waitFor(() => {
    found = latest.find(predicate);
    expect(found).toBeDefined();
  });
  return found!;
}

beforeEach(() => {
  resetTransfers();
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("reading a dataset file's header", () => {
  it("reads the first line only, and refuses another format or a newer version", async () => {
    expect(await readDatasetHeader(new Blob([FILE_TEXT]))).toEqual(HEADER);
    expect(await readDatasetHeader(new Blob(['{"_kind":"node","id":"d1"}\n']))).toBeNull();
    expect(headerProblem({ ...HEADER, _kind: "header", version: 2 })).toBe("newerVersion");
    expect(headerProblem({ ...HEADER, _kind: "header", format: "other" })).toBe("notDataset");
  });
});

/** What the transfers are, as the store says (via the indicator's subscription). */
function Watch() {
  const transfers = useTransfersForTest();
  latest = transfers;
  return null;
}
import { useTransfers as useTransfersForTest } from "../src/lib/transfers";

describe("transfers", () => {
  it("downloads into the picked file, counting rows against the header", async () => {
    render(<Watch />);
    const saved: string[] = [];
    const api = { exportDataset: vi.fn(async () => new Response(streamOf(FILE_TEXT))) } as unknown as GraphosApi;
    startDownload(
      api,
      "acme",
      { gzip: false, computed: true },
      { write: async (r) => void saved.push(await r.text()) },
    );
    const done = await settled((t) => t.phase === "done");
    expect(saved).toEqual([FILE_TEXT]);
    expect([done.filename, done.bytes, done.rows, done.totalRows]).toEqual(["acme.ndjson", FILE_TEXT.length, 5, 5]);
    expect(api.exportDataset).toHaveBeenCalledWith(expect.objectContaining({ gzip: false, computed: true }));
  });

  it("cancels a download", async () => {
    render(<Watch />);
    const api = { exportDataset: async () => new Response(streamOf(FILE_TEXT, true)) } as unknown as GraphosApi;
    const transfer = startDownload(
      api,
      "acme",
      { gzip: false, computed: true },
      { write: (r) => r.text().then(() => undefined) },
    );
    await settled((t) => t.bytes > 0);
    act(() => cancelTransfer(transfer.id));
    expect((await settled((t) => t.phase !== "receiving")).phase).toBe("cancelled");
  });

  it("uploads with progress: sending, then writing, then done", async () => {
    render(<Watch />);
    const { HttpClient } = await import("../src/api/http");
    const { GraphosApi: Api } = await import("../src/api/graphosApi");
    const api = new Api(new HttpClient("/api", "tok"), "acme");
    const onDone = vi.fn();
    const file = new File([FILE_TEXT], "acme.ndjson");
    startUpload(api, "acme", file, { mode: "empty", applySchema: false, header: null }, onDone);
    const sending = await settled((t) => t.phase === "sending" && t.bytes > 0);
    expect([sending.bytes, sending.totalBytes]).toEqual([file.size / 2, file.size]);
    const xhr = FakeXhr.last!;
    expect(xhr.url).toBe("/api/v1/acme/graph/snapshot-import?mode=empty");
    expect(xhr.headers.Authorization).toBe("Bearer tok");
    expect(xhr.body).toBe(file);
    act(() => xhr.finish());
    await settled((t) => t.phase === "writing");
    act(() => xhr.answer());
    const done = await settled((t) => t.phase === "done");
    expect(done.result).toEqual(RESULT);
    await waitFor(() => expect(onDone).toHaveBeenCalled());
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

function ui(who: Me, children: React.ReactNode) {
  const fetchImpl = vi.fn(async () => new Response("{}", { status: 200 })) as unknown as typeof fetch;
  return (
    <MantineProvider theme={theme}>
      <QueryClientProvider client={new QueryClient()}>
        <SessionProvider initial={{ me: who, tokens: TOKENS, tenant: "acme" }} fetchImpl={fetchImpl}>
          {children}
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}

function choose(text: string) {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  fireEvent.change(input, { target: { files: [new File([text], "acme.ndjson", { type: "application/x-ndjson" })] } });
}

describe("the dataset panel", () => {
  it("offers only the download to a reader", () => {
    render(ui(me("read"), <DatasetPanel />));
    expect(screen.getByRole("button", { name: "Download" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Upload" })).not.toBeInTheDocument();
  });

  it("shows an upload's progress, which survives leaving the page, and the toolbar shows it too", async () => {
    const view = render(ui(me("maintain"), <DatasetPanel />));
    choose(FILE_TEXT);
    expect(await screen.findByText(/Exported from acme, 2026-10-12T10:00:00Z: 2 nodes/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Restore into this empty tenant" }));
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));
    expect(await screen.findByText("Uploading · 50%")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload" })).toBeDisabled();

    // Another page: the section is gone, the transfer goes on.
    view.unmount();
    render(ui(me("maintain"), <TransfersIndicator />));
    expect(await screen.findByRole("button", { name: "1 dataset transfer running" })).toBeInTheDocument();
    act(() => FakeXhr.last!.finish());
    act(() => FakeXhr.last!.answer());
    await waitFor(() => expect(screen.queryByRole("button", { name: /transfer running/ })).not.toBeInTheDocument());

    // Back on the page: how it ended.
    render(ui(me("maintain"), <DatasetPanel />));
    expect(await screen.findByText("Done")).toBeInTheDocument();
    expect(screen.getByText("Imported 2 nodes and 1 relationships in 2 s.")).toBeInTheDocument();
  });

  it("does not offer the schema to a writer, and refuses a newer file", async () => {
    render(ui(me("write"), <DatasetPanel />));
    choose(FILE_TEXT);
    await screen.findByText(/Exported from acme/);
    expect(screen.queryByRole("checkbox", { name: "Register the file's schema first" })).not.toBeInTheDocument();
    choose(JSON.stringify({ ...HEADER, version: 2 }) + "\n");
    expect(await screen.findByText(/newer graphos \(format version 2\)/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload" })).toBeDisabled();
  });
});

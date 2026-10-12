import { useSyncExternalStore } from "react";
import type { GraphosApi } from "../api/graphosApi";
import type { DatasetHeader, DatasetImportResult } from "../api/types";
import { saveBlob, type SaveTarget } from "./download";

/**
 * Dataset downloads and uploads, kept outside the page that started them: a
 * transfer goes on, and its progress stays visible, when the user moves to
 * another page (only leaving the app stops it, and the app asks first).
 */
export interface Transfer {
  id: number;
  tenant: string;
  kind: "download" | "upload";
  filename: string;
  /** `sending` / `receiving` while bytes move; `writing` while graphos writes an upload. */
  phase: "receiving" | "sending" | "writing" | "done" | "failed" | "cancelled";
  bytes: number;
  /** Known for an upload (the file's size); a download's size is not known in advance. */
  totalBytes: number | null;
  /** Lines of the file seen so far (downloads), against the header's counts. */
  rows: number;
  totalRows: number | null;
  startedAt: number;
  endedAt: number | null;
  error: string | null;
  result: DatasetImportResult | null;
}

type Listener = () => void;

let transfers: Transfer[] = [];
let nextId = 1;
const listeners = new Set<Listener>();
const aborts = new Map<number, AbortController>();

function emit(): void {
  for (const listener of listeners) listener();
}

function update(id: number, change: Partial<Transfer>): void {
  transfers = transfers.map((transfer) => (transfer.id === id ? { ...transfer, ...change } : transfer));
  emit();
}

function begin(fields: Pick<Transfer, "tenant" | "kind" | "filename" | "phase" | "totalBytes">): Transfer {
  const transfer: Transfer = {
    id: nextId++,
    ...fields,
    bytes: 0,
    rows: 0,
    totalRows: null,
    startedAt: Date.now(),
    endedAt: null,
    error: null,
    result: null,
  };
  // One finished transfer of each kind per tenant is kept, to show how it ended.
  transfers = [
    ...transfers.filter((t) => t.tenant !== transfer.tenant || t.kind !== transfer.kind || isActive(t)),
    transfer,
  ];
  emit();
  return transfer;
}

function end(id: number, error: unknown): void {
  aborts.delete(id);
  if (error === undefined) {
    update(id, { phase: "done", endedAt: Date.now() });
  } else if (error instanceof DOMException && error.name === "AbortError") {
    update(id, { phase: "cancelled", endedAt: Date.now() });
  } else {
    update(id, { phase: "failed", endedAt: Date.now(), error: error instanceof Error ? error.message : String(error) });
  }
}

export function isActive(transfer: Transfer): boolean {
  return transfer.phase === "receiving" || transfer.phase === "sending" || transfer.phase === "writing";
}

/** Stops a running transfer (its result then reads "cancelled"). */
export function cancelTransfer(id: number): void {
  aborts.get(id)?.abort();
}

/** Forgets a finished transfer. */
export function dismissTransfer(id: number): void {
  transfers = transfers.filter((t) => t.id !== id || isActive(t));
  emit();
}

/** Every transfer, live. */
export function useTransfers(): Transfer[] {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => transfers,
  );
}

/** Share done, from 0 to 1, when it can be known. */
export function transferShare(transfer: Transfer): number | null {
  if (transfer.phase === "done") return 1;
  if (transfer.totalRows) return Math.min(transfer.rows / transfer.totalRows, 1);
  if (transfer.totalBytes) return Math.min(transfer.bytes / transfer.totalBytes, 1);
  return null;
}

// ── Download ───────────────────────────────────────────────────────────────

/**
 * Downloads the tenant's dataset into `target` (a file the user picked) or,
 * without one, into a Blob saved at the end. Counts the bytes received and,
 * through a second copy of the stream (decompressed when gzip), the lines
 * against the header's counts.
 */
export function startDownload(
  api: GraphosApi,
  tenant: string,
  options: { gzip: boolean; computed: boolean },
  target: SaveTarget,
): Transfer {
  const filename = `${tenant}.ndjson${options.gzip ? ".gz" : ""}`;
  const transfer = begin({ tenant, kind: "download", filename, phase: "receiving", totalBytes: null });
  const controller = new AbortController();
  aborts.set(transfer.id, controller);
  void (async () => {
    try {
      const response = await api.exportDataset({ ...options, signal: controller.signal });
      if (!response.body) throw new Error("empty response");
      const [toSave, toCount] = response.body.tee();
      const { signal } = controller;
      const counted = countLines(toCount, options.gzip, transfer.id, signal);
      const saving = target
        ? target.write(new Response(measure(toSave, transfer.id, signal)))
        : new Response(measure(toSave, transfer.id, signal)).blob().then((blob) => saveBlob(filename, blob));
      await Promise.all([saving, counted]);
      end(transfer.id, undefined);
    } catch (error) {
      end(transfer.id, controller.signal.aborted ? new DOMException("aborted", "AbortError") : error);
    }
  })();
  return transfer;
}

/** The stream, counting its bytes into the transfer; Cancel errors it (and stops its source). */
function measure(stream: ReadableStream<Uint8Array>, id: number, signal: AbortSignal): ReadableStream<Uint8Array> {
  let bytes = 0;
  return stream.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      start(controller) {
        signal.addEventListener("abort", () => controller.error(new DOMException("aborted", "AbortError")), {
          once: true,
        });
      },
      transform(chunk, controller) {
        bytes += chunk.byteLength;
        update(id, { bytes });
        controller.enqueue(chunk);
      },
    }),
  );
}

/** Counts the file's lines, reading the header's counts from the first one. */
async function countLines(
  stream: ReadableStream<Uint8Array>,
  gzip: boolean,
  id: number,
  signal: AbortSignal,
): Promise<void> {
  let source: ReadableStream<Uint8Array> = stream;
  if (gzip) {
    if (typeof DecompressionStream === "undefined") {
      await stream.cancel();
      return;
    }
    // The DOM typings type its input as any BufferSource; it gets bytes here.
    source = stream.pipeThrough(
      new DecompressionStream("gzip") as unknown as ReadableWritablePair<Uint8Array, Uint8Array>,
    );
  }
  const reader = source.getReader();
  signal.addEventListener("abort", () => void reader.cancel().catch(() => undefined), { once: true });
  const decoder = new TextDecoder();
  let rows = 0;
  let first = "";
  let lastShown = 0;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    if (rows === 0) {
      first += decoder.decode(value, { stream: true });
      const newline = first.indexOf("\n");
      if (newline >= 0) {
        const total = headerTotal(first.slice(0, newline));
        if (total !== null) update(id, { totalRows: total });
      }
    }
    // Native search for newlines: a byte-by-byte loop falls behind on a large
    // download, and the copy being saved then waits in memory for this one.
    for (let at = value.indexOf(10); at !== -1; at = value.indexOf(10, at + 1)) rows += 1;
    // At most a few updates a second: a large file has millions of lines.
    const now = Date.now();
    if (now - lastShown > 200) {
      lastShown = now;
      update(id, { rows });
    }
  }
  update(id, { rows });
}

function headerTotal(line: string): number | null {
  try {
    const header = JSON.parse(line) as Partial<DatasetHeader>;
    const counts = header._kind === "header" ? header.counts : undefined;
    // The header, every node and edge, and the end line.
    return counts ? counts.nodes + counts.edges + 2 : null;
  } catch {
    return null;
  }
}

// ── Upload ─────────────────────────────────────────────────────────────────

/**
 * Uploads `file` into the tenant, after registering the schema in its header
 * when asked; `onDone` runs once it is written (to refresh what the page shows).
 */
export function startUpload(
  api: GraphosApi,
  tenant: string,
  file: File,
  options: { mode: "merge" | "empty"; applySchema: boolean; header: DatasetHeader | null },
  onDone: () => Promise<unknown> | void,
): Transfer {
  const transfer = begin({ tenant, kind: "upload", filename: file.name, phase: "sending", totalBytes: file.size });
  const controller = new AbortController();
  aborts.set(transfer.id, controller);
  void (async () => {
    try {
      if (options.applySchema && options.header?.schema) await api.importSchema(options.header.schema);
      const result = await api.importDataset(file, options.mode, {
        signal: controller.signal,
        onSent: (bytes, total) => update(transfer.id, { bytes, phase: bytes >= total ? "writing" : "sending" }),
      });
      update(transfer.id, { result, rows: result.nodes_upserted + result.edges_upserted });
      end(transfer.id, undefined);
      await onDone();
    } catch (error) {
      end(transfer.id, error);
    }
  })();
  return transfer;
}

/** Asks before the page is left while a transfer runs: leaving would stop it. */
export function guardLeavingWhileTransferring(): () => void {
  const onBeforeUnload = (event: BeforeUnloadEvent) => {
    if (transfers.some(isActive)) {
      event.preventDefault();
      event.returnValue = "";
    }
  };
  window.addEventListener("beforeunload", onBeforeUnload);
  return () => window.removeEventListener("beforeunload", onBeforeUnload);
}

/** For tests: start from no transfers. */
export function resetTransfers(): void {
  for (const controller of aborts.values()) controller.abort();
  aborts.clear();
  transfers = [];
  emit();
}

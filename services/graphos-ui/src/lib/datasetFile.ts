import type { DatasetHeader } from "../api/types";

/** The dataset file format graphos exports, and the newest version this UI reads. */
export const DATASET_FORMAT = "graphos.snapshot";
export const DATASET_VERSION = 1;

/** A header line longer than this is not one (a schema is far smaller). */
const MAX_HEADER_BYTES = 16 * 1024 * 1024;
/** Bytes read per step while looking for the end of the first line. */
const STEP = 64 * 1024;

/**
 * Reads the header line of a dataset file (gzip or not) without reading the
 * rest: the first line, through the browser's gzip decompressor when the file
 * starts with the gzip magic bytes. `null` when the file has no header line.
 */
export async function readDatasetHeader(file: Blob): Promise<DatasetHeader | null> {
  const magic = new Uint8Array(await readBytes(file.slice(0, 2)));
  const gzip = magic[0] === 0x1f && magic[1] === 0x8b;
  const text = gzip ? await firstGzipLine(file) : await firstLine(file);
  if (text === null) return null;
  try {
    const line = JSON.parse(text) as Partial<DatasetHeader>;
    return line && line._kind === "header" ? (line as DatasetHeader) : null;
  } catch {
    return null;
  }
}

/** Why a header cannot be imported here, or null when it can. */
export function headerProblem(header: DatasetHeader): "notDataset" | "newerVersion" | null {
  if ((header.format ?? DATASET_FORMAT) !== DATASET_FORMAT) return "notDataset";
  if (typeof header.version === "number" && header.version > DATASET_VERSION) return "newerVersion";
  return null;
}

/** The first line of an uncompressed file, read in growing slices. */
async function firstLine(file: Blob): Promise<string | null> {
  const decoder = new TextDecoder();
  let text = "";
  for (let start = 0; start < Math.min(file.size, MAX_HEADER_BYTES); start += STEP) {
    text += decoder.decode(await readBytes(file.slice(start, start + STEP)), { stream: true });
    const newline = text.indexOf("\n");
    if (newline >= 0) return text.slice(0, newline);
  }
  return text || null;
}

/** The first line of a gzip file, decompressed as a stream (where the browser can). */
async function firstGzipLine(file: Blob): Promise<string | null> {
  if (typeof DecompressionStream === "undefined" || typeof file.stream !== "function") return null;
  const reader = file.stream().pipeThrough(new DecompressionStream("gzip")).getReader();
  const decoder = new TextDecoder();
  let text = "";
  try {
    while (text.length < MAX_HEADER_BYTES) {
      const { value, done } = await reader.read();
      if (value) text += decoder.decode(value, { stream: true });
      const newline = text.indexOf("\n");
      if (newline >= 0) return text.slice(0, newline);
      if (done) break;
    }
  } finally {
    void reader.cancel().catch(() => undefined);
  }
  return text || null;
}

function readBytes(blob: Blob): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error ?? new Error("could not read the file"));
    reader.readAsArrayBuffer(blob);
  });
}

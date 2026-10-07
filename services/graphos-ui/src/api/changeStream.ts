import type { GraphosApi } from "./graphosApi";
import type { ChangeItem } from "./types";

/** One Server-Sent Event: its name, id and data. */
export interface SseEvent {
  event: string;
  id: string | null;
  data: string;
}

/**
 * Splits a Server-Sent Events byte stream into events. Feed it text as it
 * arrives; it returns the events completed so far and keeps the remainder.
 */
export class SseParser {
  private buffer = "";

  push(chunk: string): SseEvent[] {
    this.buffer += chunk.replace(/\r\n?/g, "\n");
    const events: SseEvent[] = [];
    let end: number;
    while ((end = this.buffer.indexOf("\n\n")) >= 0) {
      const block = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      const event = parseBlock(block);
      if (event) events.push(event);
    }
    return events;
  }
}

function parseBlock(block: string): SseEvent | null {
  let event = "message";
  let id: string | null = null;
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "id") id = value;
    else if (field === "data") data.push(value);
  }
  return data.length || id ? { event, id, data: data.join("\n") } : null;
}

export type StreamStatus = "connecting" | "live" | "retrying" | "stopped";

export interface ChangeStreamHandlers {
  onChange: (change: ChangeItem) => void;
  onStatus?: (status: StreamStatus) => void;
}

const RETRY_DELAYS_MS = [1_000, 2_000, 5_000, 10_000, 30_000];

/**
 * Follows a tenant's change feed live from now on. Reconnects after a
 * dropped connection from the last change seen, so nothing is missed; if
 * the server has pruned that far back (`gone`), it starts again from the head.
 */
export class ChangeStream {
  private controller: AbortController | null = null;
  private cursor: string | null = null;
  private attempt = 0;

  constructor(
    private readonly api: GraphosApi,
    private readonly handlers: ChangeStreamHandlers,
  ) {}

  /** Starts following; calling it again while running does nothing. */
  start(): void {
    if (this.controller) return;
    this.controller = new AbortController();
    void this.run(this.controller.signal);
  }

  /** Stops following and closes the connection. */
  stop(): void {
    this.controller?.abort();
    this.controller = null;
    this.handlers.onStatus?.("stopped");
  }

  private async run(signal: AbortSignal): Promise<void> {
    while (!signal.aborted) {
      try {
        this.handlers.onStatus?.(this.attempt ? "retrying" : "connecting");
        this.cursor ??= await this.api.changesHead();
        const response = await this.api.openChangeStream(this.cursor, signal);
        this.handlers.onStatus?.("live");
        this.attempt = 0;
        await this.read(response, signal);
      } catch (error) {
        if (signal.aborted) return;
        console.warn("change stream interrupted", error);
      }
      if (signal.aborted) return;
      const delay = RETRY_DELAYS_MS[Math.min(this.attempt, RETRY_DELAYS_MS.length - 1)] ?? 30_000;
      this.attempt += 1;
      this.handlers.onStatus?.("retrying");
      await sleep(delay, signal);
    }
  }

  private async read(response: Response, signal: AbortSignal): Promise<void> {
    const reader = response.body?.getReader();
    if (!reader) return;
    const decoder = new TextDecoder();
    const parser = new SseParser();
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      if (done) return;
      for (const event of parser.push(decoder.decode(value, { stream: true }))) this.dispatch(event);
    }
  }

  private dispatch(event: SseEvent): void {
    if (event.event === "gone") {
      this.cursor = null;
      return;
    }
    if (event.event !== "change") return;
    if (event.id) this.cursor = event.id;
    this.handlers.onChange(JSON.parse(event.data) as ChangeItem);
  }
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

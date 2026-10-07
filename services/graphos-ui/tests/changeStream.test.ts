import { ChangeStream, SseParser } from "../src/api/changeStream";
import type { GraphosApi } from "../src/api/graphosApi";
import type { ChangeItem } from "../src/api/types";

describe("SseParser", () => {
  it("joins chunks, skips comments and keeps the event id", () => {
    const parser = new SseParser();
    expect(parser.push(": keepalive\n\nid: 5.1\nevent: chan")).toEqual([]);
    expect(parser.push('ge\ndata: {"a":1}\n\n')).toEqual([{ event: "change", id: "5.1", data: '{"a":1}' }]);
  });

  it("handles CRLF and multi-line data", () => {
    const parser = new SseParser();
    expect(parser.push("data: one\r\ndata: two\r\n\r\n")).toEqual([{ event: "message", id: null, data: "one\ntwo" }]);
  });
});

function streamOf(text: string): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text));
      controller.close();
    },
  });
  return { ok: true, status: 200, body } as unknown as Response;
}

const change = (cursor: string): ChangeItem => ({
  cursor,
  at: "2026-10-07T10:00:00Z",
  op: "patch",
  kind: "node",
  label: "Doc",
  src_label: null,
  dst_label: null,
  ids: ["d1"],
  edges: null,
  rev: 2,
  count: 1,
  consumer: "sam",
});

describe("ChangeStream", () => {
  it("starts at the head and resumes after the last change seen", async () => {
    const opened: string[] = [];
    let calls = 0;
    const api = {
      changesHead: async () => "1.0",
      openChangeStream: async (cursor: string) => {
        opened.push(cursor);
        calls += 1;
        if (calls === 1) return streamOf(`id: 2.0\nevent: change\ndata: ${JSON.stringify(change("2.0"))}\n\n`);
        return new Promise<Response>(() => undefined);
      },
    } as unknown as GraphosApi;
    const received: ChangeItem[] = [];
    const stream = new ChangeStream(api, { onChange: (c) => received.push(c) });
    stream.start();
    await vi.waitFor(() => expect(opened).toHaveLength(2), { timeout: 3_000 });
    stream.stop();
    expect(received.map((c) => c.cursor)).toEqual(["2.0"]);
    expect(opened).toEqual(["1.0", "2.0"]);
  });
});

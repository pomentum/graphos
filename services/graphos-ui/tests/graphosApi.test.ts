import { GraphosApi } from "../src/api/graphosApi";
import { HttpClient } from "../src/api/http";

function recorder() {
  const calls: { method: string; url: string; headers: Record<string, string>; body: unknown }[] = [];
  const impl = (async (url: string, init: RequestInit) => {
    calls.push({
      method: init.method ?? "GET",
      url,
      headers: init.headers as Record<string, string>,
      body: init.body ? JSON.parse(String(init.body)) : undefined,
    });
    return new Response("{}", { status: 200 });
  }) as unknown as typeof fetch;
  return { api: new GraphosApi(new HttpClient("/api", "t", impl), "demo co"), calls };
}

describe("GraphosApi", () => {
  it("builds tenant-scoped paths, encoding names and ids", async () => {
    const { api, calls } = recorder();
    await api.schema();
    await api.node("fn:a/b", "Function");
    await api.search({ q: "get user", index: "names", limit: 5 });
    expect(calls.map((c) => c.url)).toEqual([
      "/api/v1/demo%20co/schema",
      "/api/v1/demo%20co/graph/nodes/fn%3Aa%2Fb?label=Function",
      "/api/v1/demo%20co/graph/search?q=get+user&index=names&limit=5",
    ]);
  });

  it("patches with merge-patch and If-Match", async () => {
    const { api, calls } = recorder();
    await api.patchNode("n1", { owner: null }, { label: "Doc", etag: '"7.2"' });
    expect(calls[0]).toMatchObject({ method: "PATCH", body: { owner: null } });
    expect(calls[0]?.headers["If-Match"]).toBe('"7.2"');
    expect(calls[0]?.headers["Content-Type"]).toBe("application/merge-patch+json");
  });

  it("addresses one edge by type and both ends", async () => {
    const { api, calls } = recorder();
    await api.deleteEdge({ type: "CALLS", srcId: "a", srcLabel: "Function", dstId: "b", dstLabel: "Function" });
    expect(calls[0]).toMatchObject({
      method: "DELETE",
      url: "/api/v1/demo%20co/graph/edges/CALLS?src=a&dst=b&src_label=Function&dst_label=Function",
    });
  });

  it("samples a label's nodes with every property", async () => {
    const { api, calls } = recorder();
    await api.sampleNodes("Doc", 50);
    expect(calls[0]).toMatchObject({ method: "POST", body: { labels: ["Doc"], project: ["*"], limit: 50 } });
  });
});

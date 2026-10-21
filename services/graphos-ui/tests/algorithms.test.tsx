import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Job, Me } from "../src/api/types";
import { communityAlgorithms, groupsRun } from "../src/lib/features";
import { isComputedProperty } from "../src/lib/properties";
import { formatMetric, hasMetrics, largestValue, metricSize } from "../src/lib/metrics";
import { SessionProvider } from "../src/session/SessionProvider";
import { theme } from "../src/theme/mantineTheme";
import { LensHelp } from "../src/views/explore/LensSwitch";
import { GraphAlgorithms } from "../src/views/overview/GraphAlgorithms";

describe("metric helpers", () => {
  it("sizes a node by its share of the largest value, smallest without one", () => {
    expect(metricSize(1, 1)).toBe(16);
    expect(metricSize(0.25, 1)).toBeCloseTo(3 + 13 * 0.5);
    expect(metricSize(undefined, 1)).toBe(3);
    expect(metricSize(0.5, 0)).toBe(3);
    expect(largestValue([0.1, "x", 0.4, undefined])).toBe(0.4);
  });

  it("reads scores to four significant digits and counts as they are", () => {
    expect(formatMetric(0.106703)).toBe("0.1067");
    expect(formatMetric(21)).toBe("21");
    expect(formatMetric(0)).toBe("0");
    expect(formatMetric("21")).toBe("");
    expect(hasMetrics({ graphos_pagerank: 0.1 })).toBe(true);
    expect(hasMetrics({ name: "x", community_id: 3 })).toBe(false);
  });

  it("keeps computed values apart from the properties one edits", () => {
    expect(isComputedProperty("graphos_pagerank")).toBe(true);
    expect(isComputedProperty("community_id")).toBe(true);
    expect(isComputedProperty("name")).toBe(false);
    expect(isComputedProperty("id")).toBe(false);
  });
});

const TOKENS = { access_token: "a1", expires_in: 900, refresh_token: "r1", must_change: false };

function me(level: "maintain" | "read"): Me {
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

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/**
 * A graphos with or without the Louvain extra, recording what was sent;
 * `algorithms` and `tenantAlgorithm` as a 0.14.0 server reports them, `jobs` its recent jobs.
 */
function server({
  louvain,
  algorithms,
  tenantAlgorithm,
  jobs = [],
}: {
  louvain: boolean;
  algorithms?: string[];
  tenantAlgorithm?: string;
  jobs?: Partial<Job>[];
}) {
  const sent: { method: string; path: string; body: unknown; prefer: string | null }[] = [];
  const fetchImpl = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input), "http://ui");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    const prefer = new Headers(init?.headers).get("Prefer");
    sent.push({ method, path: url.pathname, body, prefer });
    if (url.pathname === "/api/v1/info") {
      const features = { louvain, metrics: true, ...(algorithms ? { community_algorithms: algorithms } : {}) };
      return json({ service: "graphos", version: "0.14.1", features });
    }
    if (url.pathname === "/api/v1/acme/graph/communities/map") {
      const done = jobs.some((j) => j.kind === "recompute_communities");
      return json({
        tenant: "acme",
        available: done,
        n_communities: done ? 6 : 0,
        modularity: done ? 0.74 : null,
        communities: [],
        links: [],
      });
    }
    if (url.pathname === "/api/v1/jobs") return json({ jobs });
    if (url.pathname === "/api/v1/acme/schema") {
      return json({
        name: "acme",
        vertex_labels: ["Package"],
        edge_labels: ["DEPENDS_ON", "TAGGED"],
        label_hierarchy: {},
        search_indexes: {},
        rule_edge_labels: [],
        edge_endpoints: {},
        ...(tenantAlgorithm ? { community_algorithm: tenantAlgorithm } : {}),
      });
    }
    if (url.pathname === "/api/v1/acme/recompute-communities" && method === "POST") {
      return json({ id: "j2", kind: "recompute_communities", tenant: "acme", status: "queued" }, 202);
    }
    if (url.pathname === "/api/v1/acme/graph/metrics:compute" && method === "POST") {
      return json({ id: "j1", kind: "compute_metrics", tenant: "acme", status: "queued" }, 202);
    }
    return json({ detail: "unexpected" }, 404);
  }) as unknown as typeof fetch;
  return { fetchImpl, sent };
}

function renderCard(who: Me, fetchImpl: typeof fetch) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider theme={theme}>
      <QueryClientProvider client={client}>
        <SessionProvider initial={{ me: who, tokens: TOKENS, tenant: "acme" }} fetchImpl={fetchImpl}>
          <GraphAlgorithms />
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

describe("Graph algorithms", () => {
  it("without Louvain on the server, says so instead of offering the groups", async () => {
    const { fetchImpl } = server({ louvain: false });
    renderCard(me("maintain"), fetchImpl);
    expect(await screen.findByText(/Needs the Louvain extra/)).toBeInTheDocument();
    // The only Compute button left is the metrics one.
    expect(screen.getAllByRole("button", { name: "Compute" })).toHaveLength(1);
  });

  it("computes the metrics as a background job, with betweenness when asked", async () => {
    const { fetchImpl, sent } = server({ louvain: true });
    renderCard(me("maintain"), fetchImpl);
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Compute" })).toHaveLength(2));
    fireEvent.click(screen.getByLabelText(/Include betweenness/));
    const buttons = screen.getAllByRole("button", { name: "Compute" });
    fireEvent.click(buttons[buttons.length - 1]!);
    await waitFor(() => expect(sent.some((s) => s.path.endsWith("/metrics:compute"))).toBe(true));
    const call = sent.find((s) => s.path.endsWith("/metrics:compute"))!;
    expect(call.body).toEqual({ metrics: ["degree", "pagerank", "components", "betweenness"] });
    expect(call.prefer).toBe("respond-async");
  });

  it("computes over the relationship types chosen", async () => {
    const { fetchImpl, sent } = server({ louvain: true });
    renderCard(me("maintain"), fetchImpl);
    // The types come from the schema; the list renders hidden until opened (jsdom cannot scroll it).
    fireEvent.click(await screen.findByRole("option", { name: "DEPENDS_ON", hidden: true }));
    const buttons = screen.getAllByRole("button", { name: "Compute" });
    fireEvent.click(buttons[buttons.length - 1]!);
    await waitFor(() => expect(sent.some((s) => s.path.endsWith("/metrics:compute"))).toBe(true));
    expect(sent.find((s) => s.path.endsWith("/metrics:compute"))!.body).toEqual({
      metrics: ["degree", "pagerank", "components"],
      rel_types: ["DEPENDS_ON"],
    });
  });

  it("recomputes the groups with the tenant's algorithm unless another is chosen", async () => {
    const { fetchImpl, sent } = server({
      louvain: true,
      algorithms: ["louvain", "leiden"],
      tenantAlgorithm: "louvain",
    });
    renderCard(me("maintain"), fetchImpl);
    expect(await screen.findByText("The tenant runs Louvain unless another is chosen.")).toBeInTheDocument();
    expect(screen.getByLabelText("Tenant's (Louvain)")).toBeChecked();
    // Louvain always starts from scratch: no choice to make.
    expect(screen.queryByLabelText("From scratch")).toBeNull();
    fireEvent.click(screen.getAllByRole("button", { name: "Compute" })[0]!);
    await waitFor(() => expect(sent.some((s) => s.path.endsWith("/recompute-communities"))).toBe(true));
    const call = sent.find((s) => s.path.endsWith("/recompute-communities"))!;
    expect(call.body).toEqual({});
    expect(call.prefer).toBe("respond-async");
  });

  it("recomputes with Leiden, from scratch when asked", async () => {
    const { fetchImpl, sent } = server({
      louvain: true,
      algorithms: ["louvain", "leiden"],
      tenantAlgorithm: "louvain",
    });
    renderCard(me("maintain"), fetchImpl);
    fireEvent.click(await screen.findByLabelText("Leiden"));
    fireEvent.click(await screen.findByLabelText("From scratch"));
    fireEvent.click(screen.getAllByRole("button", { name: "Compute" })[0]!);
    await waitFor(() => expect(sent.some((s) => s.path.endsWith("/recompute-communities"))).toBe(true));
    expect(sent.find((s) => s.path.endsWith("/recompute-communities"))!.body).toEqual({
      algorithm: "leiden",
      from_scratch: true,
    });
  });

  it("says how the last recompute ran", async () => {
    const job: Partial<Job> = {
      id: "j0",
      kind: "recompute_communities",
      tenant: "acme",
      status: "succeeded",
      params: {},
      result: { algorithm: "leiden", from_scratch: false, n_communities: 6 },
      error: null,
      finished_at: new Date().toISOString(),
    };
    const { fetchImpl } = server({
      louvain: true,
      algorithms: ["louvain", "leiden"],
      tenantAlgorithm: "leiden",
      jobs: [job],
    });
    renderCard(me("maintain"), fetchImpl);
    expect(await screen.findByText(/Leiden, from the stored groups/)).toBeInTheDocument();
    expect(screen.getByText("The tenant runs Leiden unless another is chosen.")).toBeInTheDocument();
  });

  it("keeps the algorithm the last recompute ran chosen, so Compute again repeats it", async () => {
    const job: Partial<Job> = {
      id: "j0",
      kind: "recompute_communities",
      tenant: "acme",
      status: "succeeded",
      params: {},
      result: { algorithm: "leiden", from_scratch: true, n_communities: 6 },
      error: null,
      finished_at: new Date().toISOString(),
    };
    const { fetchImpl, sent } = server({
      louvain: true,
      algorithms: ["louvain", "leiden"],
      tenantAlgorithm: "louvain",
      jobs: [job],
    });
    renderCard(me("maintain"), fetchImpl);
    expect(await screen.findByText(/Leiden, from scratch/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Leiden")).toBeChecked());
    expect(screen.getByLabelText("Tenant's (Louvain)")).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Compute again" }));
    await waitFor(() => expect(sent.some((s) => s.path.endsWith("/recompute-communities"))).toBe(true));
    expect(sent.find((s) => s.path.endsWith("/recompute-communities"))!.body).toEqual({ algorithm: "leiden" });
  });

  it("offers no choice on a server with Louvain only", async () => {
    const { fetchImpl } = server({ louvain: true });
    renderCard(me("maintain"), fetchImpl);
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Compute" })).toHaveLength(2));
    expect(screen.queryByLabelText("Leiden")).toBeNull();
    expect(screen.queryByText(/unless another is chosen/)).toBeNull();
  });

  it("offers nothing to run to a reader", async () => {
    const { fetchImpl } = server({ louvain: true });
    renderCard(me("read"), fetchImpl);
    expect(await screen.findByText("Graph algorithms")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText("Not computed yet.").length).toBeGreaterThan(0));
    expect(screen.queryByRole("button", { name: /Compute/ })).toBeNull();
  });
});

describe("community algorithm helpers", () => {
  it("knows which algorithms the server runs", () => {
    expect(communityAlgorithms(undefined)).toEqual([]);
    expect(communityAlgorithms({ louvain: false, metrics: true })).toEqual([]);
    expect(communityAlgorithms({ louvain: true, metrics: true })).toEqual(["louvain"]);
    expect(communityAlgorithms({ louvain: true, metrics: true, community_algorithms: ["louvain", "leiden"] })).toEqual([
      "louvain",
      "leiden",
    ]);
  });

  it("reads how the last recompute ran from its job, Louvain from scratch on an older server", () => {
    const base = { id: "j", kind: "recompute_communities", status: "succeeded", params: {}, error: null } as const;
    expect(groupsRun({ ...base, result: { algorithm: "leiden", from_scratch: false } } as unknown as Job)).toEqual({
      algorithm: "leiden",
      fromScratch: false,
    });
    expect(groupsRun({ ...base, result: { n_communities: 3 } } as unknown as Job)).toEqual({
      algorithm: "louvain",
      fromScratch: true,
    });
    expect(groupsRun({ ...base, status: "failed", result: null } as unknown as Job)).toBeNull();
    expect(groupsRun(null)).toBeNull();
  });
});

describe("view help", () => {
  it("says what a metric view shows and how to read it", async () => {
    render(
      <MantineProvider theme={theme}>
        <LensHelp lens="graphos_betweenness" />
      </MantineProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "What this view shows" }));
    expect(await screen.findByText("Betweenness")).toBeInTheDocument();
    expect(screen.getByText(/bridge or a bottleneck/)).toBeInTheDocument();
    expect(screen.getByText("How to read it")).toBeInTheDocument();
  });
});

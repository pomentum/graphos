import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { CsvImportResult, Me, PropertyDefinitions, TenantSchema } from "../src/api/types";
import { SessionProvider } from "../src/session/SessionProvider";
import { theme } from "../src/theme/mantineTheme";
import { CsvImportDialog } from "../src/views/overview/CsvImportDialog";
import { DatasetPanel } from "../src/views/overview/DatasetPanel";

const TOKENS = { access_token: "a1", expires_in: 900, refresh_token: "r1", must_change: false };

/** Files whose reading waits for the test to let it finish, by name. */
const held = vi.hoisted(() => new Map<string, Promise<void>>());
vi.mock("../src/lib/csv", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/csv")>();
  return {
    ...actual,
    readFileText: async (file: Blob) => {
      await held.get((file as File).name);
      return actual.readFileText(file);
    },
  };
});

/** Holds the reading of a file until the returned function is called. */
function hold(name: string): () => void {
  let release = () => undefined as void;
  held.set(name, new Promise<void>((resolve) => (release = resolve)));
  return () => {
    held.delete(name);
    release();
  };
}

const SCHEMA: TenantSchema = {
  name: "acme",
  vertex_labels: ["Company", "Person"],
  edge_labels: ["DERIVED", "WORKS_AT"],
  label_hierarchy: {},
  search_indexes: {},
  rule_edge_labels: ["DERIVED"],
  edge_endpoints: { WORKS_AT: [{ src: "Person", dst: "Company" }] },
  strict_endpoints: false,
};

const DEFINITIONS: PropertyDefinitions = {
  tenant: "acme",
  mode: "warn",
  vertex: {
    Person: {
      mode: "warn",
      undeclared: "allow",
      undeclared_source: "*",
      properties: { age: { source: "Person", definition: { type: "integer" } } },
    },
  },
  edge: {},
};

/** An answer of `nodes:csv` / `edges:csv`, nothing to report unless said. */
function answer(extra: Partial<CsvImportResult> = {}): CsvImportResult {
  return {
    tenant: "acme",
    dry_run: true,
    delimiter: "comma",
    rows: 2,
    records: 2,
    rows_invalid: 0,
    written: 2,
    edges_skipped: 0,
    columns: [],
    issues: [],
    errors: [],
    elapsed_ms: 1200,
    property_violations: {},
    property_warnings: {},
    ...extra,
  };
}

type Reply = { status: number; body: unknown } | null;

/** How graphos answers an upload; `null` holds it for the test to answer. Every dry run passes by default. */
let reply: (xhr: FakeXhr) => Reply = () => null;

/** An `XMLHttpRequest`: half the file goes at once, then graphos answers as `reply` says. */
class FakeXhr {
  static all: FakeXhr[] = [];
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
  body: Blob | null = null;
  aborted = false;
  open(_method: string, url: string) {
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }
  send(body: Blob) {
    this.body = body;
    FakeXhr.all.push(this);
    this.upload.onprogress?.({ loaded: body.size / 2, total: body.size, lengthComputable: true });
    const now = reply(this);
    if (now) setTimeout(() => this.answer(now.body, now.status), 0);
  }
  /** The rest of the file goes. */
  finish() {
    const size = this.body!.size;
    this.upload.onprogress?.({ loaded: size, total: size, lengthComputable: true });
    this.upload.onload?.();
  }
  answer(body: unknown, status = 200) {
    if (this.aborted) return;
    this.finish();
    this.status = status;
    this.responseText = JSON.stringify(body);
    this.onload?.();
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
  get path(): string {
    return new URL(this.url, "http://ui").pathname;
  }
  get query(): URLSearchParams {
    return new URL(this.url, "http://ui").searchParams;
  }
  get dryRun(): boolean {
    return this.query.get("dry_run") === "true";
  }
}

const dryRuns = () => FakeXhr.all.filter((xhr) => xhr.dryRun);
const imports = () => FakeXhr.all.filter((xhr) => !xhr.dryRun);

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function me(level: "read" | "write"): Me {
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

/** A graphos answering the schema, the property definitions and the counts. */
const fetchImpl = vi.fn(async (input: string | URL | Request) => {
  const path = new URL(String(input), "http://ui").pathname;
  if (path === "/api/v1/acme/schema") return json(SCHEMA);
  if (path === "/api/v1/acme/graph/properties") return json(DEFINITIONS);
  return json({});
}) as unknown as typeof fetch;

function ui(children: React.ReactNode, level: "read" | "write" = "write") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <MantineProvider theme={theme}>
      <QueryClientProvider client={client}>
        <SessionProvider initial={{ me: me(level), tokens: TOKENS, tenant: "acme" }} fetchImpl={fetchImpl}>
          {children}
        </SessionProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}

function choose(text: string | null, name = "people.csv") {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  const files = text === null ? [] : [new File([text], name, { type: "text/csv" })];
  fireEvent.change(input, { target: { files } });
}

function schemaReads(): number {
  return vi
    .mocked(fetchImpl)
    .mock.calls.filter(([input]) => new URL(String(input), "http://ui").pathname === "/api/v1/acme/schema").length;
}

/** "3 columns · 40 B (the first row is the header)", whatever the size. */
function detected(columns: number): RegExp {
  return new RegExp(`^${columns} columns · .+ \\(the first row is the header\\)$`);
}

beforeEach(() => {
  FakeXhr.all = [];
  reply = (xhr) => (xhr.dryRun ? { status: 200, body: answer() } : null);
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

/** Waits for the file to be read, a dry run's pause, and the dialog to draw again, slowly while the whole suite runs. */
const READ = { timeout: 5000 };

describe("the CSV import", { timeout: 20_000 }, () => {
  it("is offered beside the dataset upload to writers only", async () => {
    const view = render(ui(<DatasetPanel />, "read"));
    expect(screen.queryByRole("button", { name: "Import CSV" })).not.toBeInTheDocument();
    view.unmount();
    render(ui(<DatasetPanel />, "write"));
    fireEvent.click(screen.getByRole("button", { name: "Import CSV" }));
    expect(await screen.findByLabelText("CSV file")).toBeInTheDocument();
  });

  it("has graphos check the file as mapped, again when the mapping changes, then sends it to nodes:csv", async () => {
    const dry = answer({
      delimiter: "semicolon",
      rows: 3,
      records: 3,
      rows_invalid: 1,
      written: 2,
      issues: [
        { code: "empty_id", refused: true, count: 1, examples: [{ row: 3, column: "id" }] },
        { code: "bad_value", refused: false, count: 1, examples: [{ row: 2, column: "Age", value: "old" }] },
        { code: "ragged", refused: false, count: 1, examples: [{ row: 1, cells: 3 }] },
        { code: "brand_new", refused: false, count: 2, examples: [] },
      ],
      errors: ["row 3: id: String should have at least 1 character"],
      property_warnings: { "Person.age:type": 1 },
    });
    reply = (xhr) => (xhr.dryRun && xhr.query.get("label") === "Person" ? { status: 200, body: dry } : null);
    render(ui(<CsvImportDialog onClose={() => undefined} />));
    choose("\uFEFFname;id;Age;graphos_degree\r\nAda;p1;36;3\r\nBob;p2;old;1\r\nCy;;7;2\r\n");

    expect(await screen.findByText(detected(4), {}, READ)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Semicolon" })).toBeChecked();
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Label" })).toHaveValue("Company"));
    expect(screen.getByRole("textbox", { name: "Id column" })).toHaveValue("id");
    await waitFor(() => expect(dryRuns()).toHaveLength(1), READ);
    expect(screen.getByText("Sending the file for graphos to check · 50%")).toBeInTheDocument();
    const first = dryRuns()[0]!;
    expect(first.path).toBe("/api/v1/acme/graph/nodes:csv");
    expect(first.headers["Content-Type"]).toBe("text/csv");
    expect((first.body as File).name).toBe("people.csv");

    // Another label: the stale check is dropped, graphos checks again.
    fireEvent.click(screen.getByRole("option", { name: "Person", hidden: true }));
    await waitFor(() => expect(first.aborted).toBe(true));
    await waitFor(() => expect(dryRuns()).toHaveLength(2), READ);
    const second = dryRuns()[1]!.query;
    expect([second.get("label"), second.get("id"), second.get("delimiter")]).toEqual(["Person", "id", "semicolon"]);
    expect(JSON.parse(second.get("columns")!)).toEqual({
      name: { property: "name", type: "auto" },
      Age: { property: "Age", type: "auto" },
      graphos_degree: { property: null, type: "auto" },
    });

    expect(await screen.findByText("graphos's check of all 3 rows", {}, READ)).toBeInTheDocument();
    expect(
      screen.getByText("They make 3 records once rows repeating an id are merged; 2 would be written."),
    ).toBeInTheDocument();
    expect(screen.getByText("1 record would be refused")).toBeInTheDocument();
    expect(screen.getByText("1 empty id")).toBeInTheDocument();
    expect(screen.getByText('For example: row 2 Age "old"')).toBeInTheDocument();
    expect(screen.getByText("For example: row 1 (3 cells)")).toBeInTheDocument();
    expect(screen.getByText("2 × brand_new")).toBeInTheDocument();
    expect(screen.getByText("Written with property warnings: Person.age:type (1)")).toBeInTheDocument();
    expect(screen.getByText("row 3: id: String should have at least 1 character")).toBeInTheDocument();

    // The declared name and type: checked again with them.
    fireEvent.change(screen.getByRole("textbox", { name: "Property name for Age" }), { target: { value: "age" } });
    await waitFor(() => expect(dryRuns()).toHaveLength(3), READ);
    expect(JSON.parse(dryRuns()[2]!.query.get("columns")!).Age).toEqual({ property: "age", type: "number" });
    expect(screen.getByTitle("Does not read as Number: left out")).toHaveTextContent("old");

    fireEvent.click(await screen.findByRole("button", { name: "Import 3 rows" }, READ));
    expect(await screen.findByText("Sending · 50%", {}, READ)).toBeInTheDocument();
    const sent = imports()[0]!;
    expect(sent.path).toBe("/api/v1/acme/graph/nodes:csv");
    expect(sent.query.get("dry_run")).toBe("false");
    expect(sent.query.get("label")).toBe("Person");
    expect(sent.headers["Content-Type"]).toBe("text/csv");
    act(() => sent.answer({ ...dry, dry_run: false }));
    expect(
      await screen.findByText("3 rows, 3 records once rows repeating an id are merged; 2 written, in 1 s.", {}, READ),
    ).toBeInTheDocument();
    expect(screen.getByText("1 record refused")).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
  });

  it("shows graphos's refusal of the mapping, and a file it cannot read", async () => {
    reply = () => ({ status: 422, body: { detail: "column 'Age': 'bad name' is not a property name" } });
    render(ui(<CsvImportDialog onClose={() => undefined} />));
    choose("id,Age\np1,3\n");
    expect(
      await screen.findByText(
        "graphos refuses this mapping: column 'Age': 'bad name' is not a property name",
        {},
        READ,
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();

    reply = () => ({ status: 400, body: { detail: "a quoted cell is never closed" } });
    choose("id,Age\np2,4\n", "other.csv");
    expect(
      await screen.findByText("graphos couldn't check the file: a quoted cell is never closed", {}, READ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();
  });

  it("sends relationships to edges:csv with their type, labels and end columns", async () => {
    reply = (xhr) =>
      xhr.dryRun && xhr.path.endsWith("edges:csv")
        ? { status: 200, body: answer({ written: 1, edges_skipped: 1 }) }
        : null;
    render(ui(<CsvImportDialog onClose={() => undefined} />));
    choose("from,to,since\np1,c1,2020\np2,c9,2021\n", "jobs.csv");
    expect(await screen.findByText(detected(3), {}, READ)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "A relationship" }));

    // Rule-derived types are not offered; the declared pair is the default.
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Relationship type" })).toHaveValue("WORKS_AT"));
    expect(screen.queryByRole("option", { name: "DERIVED", hidden: true })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Source label" })).toHaveValue("Person");
    expect(screen.getByRole("textbox", { name: "Target label" })).toHaveValue("Company");
    expect(
      await screen.findByText("1 relationship would be skipped: its source or target node does not exist", {}, READ),
    ).toBeInTheDocument();
    const query = dryRuns().at(-1)!.query;
    expect(Object.fromEntries(query)).toEqual({
      type: "WORKS_AT",
      src_label: "Person",
      dst_label: "Company",
      src: "from",
      dst: "to",
      columns: JSON.stringify({ since: { property: "since", type: "auto" } }),
      delimiter: "comma",
      dry_run: "true",
    });

    fireEvent.click(screen.getByRole("button", { name: "Import 2 rows" }));
    await waitFor(() => expect(imports()).toHaveLength(1));
    expect(imports()[0]!.path).toBe("/api/v1/acme/graph/edges:csv");
    act(() => imports()[0]!.answer(answer({ dry_run: false, written: 1, edges_skipped: 1 })));
    expect(
      await screen.findByText("1 relationship skipped: its source or target node does not exist", {}, READ),
    ).toBeInTheDocument();
  });

  it("says nothing is written when cancelled while sending, and that graphos may go on once it has the file", async () => {
    render(ui(<CsvImportDialog onClose={() => undefined} />));
    choose("id\np1\np2\n");
    fireEvent.click(await screen.findByRole("button", { name: "Import 2 rows" }, READ));
    expect(await screen.findByText("Sending · 50%", {}, READ)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(
      await screen.findByText(
        "Cancelled while sending the file: graphos writes nothing before it has all of it",
        {},
        READ,
      ),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Import 2 rows" }));
    await waitFor(() => expect(imports()).toHaveLength(2));
    act(() => imports()[1]!.finish());
    expect(await screen.findByText("graphos is writing the rows", {}, READ)).toBeInTheDocument();
    const before = schemaReads();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(
      await screen.findByText("Stopped waiting: graphos had the whole file and may still be writing it", {}, READ),
    ).toBeInTheDocument();
    await waitFor(() => expect(schemaReads()).toBeGreaterThan(before));
  });

  it("reads the start again with another separator, and graphos is told the one chosen", async () => {
    render(ui(<CsvImportDialog onClose={() => undefined} />));
    choose("a;b\n1;2\n3;4\n");
    expect(await screen.findByText(detected(2), {}, READ)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Semicolon" })).toBeChecked();
    fireEvent.click(screen.getByRole("radio", { name: "Comma" }));
    expect(await screen.findByText(detected(1), {}, READ)).toBeInTheDocument();
    await waitFor(() => expect(dryRuns().at(-1)?.query.get("delimiter")).toBe("comma"), READ);
    choose("only,a,header\n");
    expect(await screen.findByText("The file has no rows under its header row.", {}, READ)).toBeInTheDocument();
  });

  it("drops a file's read when another file was chosen, or the choice cleared, before it finished", async () => {
    render(ui(<CsvImportDialog onClose={() => undefined} />));
    const releaseSlow = hold("slow.csv");
    choose("a,b,c\n1,2,3\n", "slow.csv");
    choose("x,y\n1,2\n3,4\n", "fast.csv");
    expect(await screen.findByText(detected(2), {}, READ)).toBeInTheDocument();
    await act(async () => releaseSlow());
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(screen.getByText(detected(2))).toBeInTheDocument();
    expect(screen.queryByText(detected(3))).not.toBeInTheDocument();

    const releaseCleared = hold("cleared.csv");
    choose("a,b,c\n1,2,3\n", "cleared.csv");
    choose(null);
    await act(async () => releaseCleared());
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(screen.queryByText(/columns · /)).not.toBeInTheDocument();
  });
});

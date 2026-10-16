import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { BulkEdgesResult, BulkNodesResult, Me, PropertyDefinitions, TenantSchema } from "../src/api/types";
import { readFileText } from "../src/lib/csv";
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

const NODES_RESULT: BulkNodesResult = {
  tenant: "acme",
  rows_received: 3,
  rows_invalid: 1,
  nodes_upserted: 2,
  by_label: { Person: 2 },
  errors: ["line 3: id: String should have at least 1 character"],
  elapsed_ms: 1200,
  property_violations: {},
  property_warnings: { "Person.age:type": 1 },
};

const EDGES_RESULT: BulkEdgesResult = {
  tenant: "acme",
  rows_received: 2,
  rows_invalid: 0,
  edges_upserted: 1,
  edges_skipped: 1,
  by_type: { WORKS_AT: 1 },
  errors: [],
  elapsed_ms: 300,
};

/** An `XMLHttpRequest` that sends at once and answers what the test says. */
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
  body: Blob | null = null;
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
  answer(result: unknown) {
    this.upload.onload?.();
    this.status = 200;
    this.responseText = JSON.stringify(result);
    this.onload?.();
  }
  abort() {
    this.onabort?.();
  }
}

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

async function sentLines(): Promise<unknown[]> {
  const body = await readFileText(FakeXhr.last!.body!);
  return body
    .trimEnd()
    .split("\n")
    .map((line) => JSON.parse(line));
}

beforeEach(() => {
  FakeXhr.last = null;
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

/** Waits for the file to be read and the dialog to draw again, slowly while the whole suite runs. */
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

  it("maps a file's rows to nodes, previews and checks them, and sends them as NDJSON", async () => {
    const onClose = vi.fn();
    render(ui(<CsvImportDialog onClose={onClose} />));
    choose("\uFEFFname;id;Age;graphos_degree\r\nAda;p1;36;3\r\nBob;p2;old;1\r\nCy;;7;2\r\n");

    expect(await screen.findByText("3 rows · 4 columns (the first row is the header)", {}, READ)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Semicolon" })).toBeChecked();
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Label" })).toHaveValue("Company"));
    expect(screen.getByRole("textbox", { name: "Id column" })).toHaveValue("id");

    fireEvent.click(screen.getByRole("option", { name: "Person", hidden: true }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Label" })).toHaveValue("Person"));
    // The header made a property name; its type from Person's definitions; graphos's own names left out.
    expect(screen.getByRole("textbox", { name: "Property name for Age" })).toHaveValue("Age");
    fireEvent.change(screen.getByRole("textbox", { name: "Property name for Age" }), { target: { value: "age" } });
    expect(screen.getByText(/^Checking the rows · \d+%$/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Type of Age" })).toHaveValue("Number"));
    expect(screen.getByRole("checkbox", { name: "Import graphos_degree" })).not.toBeChecked();
    expect(screen.getByText("Declared: integer")).toBeInTheDocument();

    expect(screen.getByText("The first 3 of 3 rows, as they will be written.")).toBeInTheDocument();
    expect(screen.getByTitle("Does not read as Number: left out")).toHaveTextContent("old");
    expect(await screen.findByText("1 empty id", {}, READ)).toBeInTheDocument();
    expect(screen.getByText("1 value does not read as its column's type: it is left out")).toBeInTheDocument();
    expect(screen.getByText('For example: row 2 age "old"')).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Import 3 rows" }));
    expect(await screen.findByText("Sending · 50%", {}, READ)).toBeInTheDocument();
    const xhr = FakeXhr.last!;
    expect(xhr.url).toBe("/api/v1/acme/graph/nodes:bulk");
    expect(xhr.headers["Content-Type"]).toBe("application/x-ndjson");
    expect(await sentLines()).toEqual([
      { id: "p1", label: "Person", properties: { name: "Ada", age: 36 } },
      { id: "p2", label: "Person", properties: { name: "Bob" } },
      { id: "", label: "Person", properties: { name: "Cy", age: 7 } },
    ]);
    act(() => xhr.answer(NODES_RESULT));
    expect(await screen.findByText("3 rows received, 2 written, in 1 s.", {}, READ)).toBeInTheDocument();
    expect(screen.getByText("1 row refused")).toBeInTheDocument();
    expect(screen.getByText("Written with property warnings: Person.age:type (1)")).toBeInTheDocument();
    expect(screen.getByText("Row 3: id: String should have at least 1 character")).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
  });

  it("sends rows with the same id as one node and names graphos's refusals by the file's row", async () => {
    render(ui(<CsvImportDialog onClose={() => undefined} />));
    choose("id,name,city\na,Ada,Paris\n a ,,Lyon\n  ,Nobody,\nb,Bob,\n");
    expect(await screen.findByText("4 rows · 3 columns (the first row is the header)", {}, READ)).toBeInTheDocument();
    expect(
      await screen.findByText(
        "1 row repeats the id of an earlier row: rows with the same id are sent as one node, a later non-empty cell winning",
        {},
        READ,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("1 empty id")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Import 4 rows" }));
    await waitFor(() => expect(FakeXhr.last).not.toBeNull());
    expect(await sentLines()).toEqual([
      { id: "a", label: "Company", properties: { name: "Ada", city: "Lyon" } },
      { id: "", label: "Company", properties: { name: "Nobody" } },
      { id: "b", label: "Company", properties: { name: "Bob" } },
    ]);
    act(() =>
      FakeXhr.last!.answer({
        ...NODES_RESULT,
        rows_received: 3,
        errors: ["line 2: id: String should have at least 1 character"],
      }),
    );
    expect(
      await screen.findByText(
        "The file's 4 rows were sent as 3: rows with the same id, or the same source and target, merged into one.",
        {},
        READ,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Row 3: id: String should have at least 1 character")).toBeInTheDocument();
  });

  it("says rows may be written when stopped while sending, and shows the new data", async () => {
    render(ui(<CsvImportDialog onClose={() => undefined} />));
    choose("id\np1\np2\n");
    expect(await screen.findByText("2 rows · 1 columns (the first row is the header)", {}, READ)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Import 2 rows" }));
    expect(await screen.findByText("Sending · 50%", {}, READ)).toBeInTheDocument();
    const before = schemaReads();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(
      await screen.findByText("Stopped: rows sent before stopping may already be written", {}, READ),
    ).toBeInTheDocument();
    await waitFor(() => expect(schemaReads()).toBeGreaterThan(before));
  });

  it("drops a file's read when another file was chosen, or the choice cleared, before it finished", async () => {
    render(ui(<CsvImportDialog onClose={() => undefined} />));
    const releaseSlow = hold("slow.csv");
    choose("a,b,c\n1,2,3\n", "slow.csv");
    choose("x,y\n1,2\n3,4\n", "fast.csv");
    expect(await screen.findByText("2 rows · 2 columns (the first row is the header)", {}, READ)).toBeInTheDocument();
    await act(async () => releaseSlow());
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(screen.getByText("2 rows · 2 columns (the first row is the header)")).toBeInTheDocument();
    expect(screen.queryByText(/^1 row · 3 columns/)).not.toBeInTheDocument();

    const releaseCleared = hold("cleared.csv");
    choose("a,b,c\n1,2,3\n", "cleared.csv");
    choose(null);
    await act(async () => releaseCleared());
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(screen.queryByText(/columns \(the first row is the header\)/)).not.toBeInTheDocument();
  });

  it("maps rows to relationships between declared labels and reports the skipped ones", async () => {
    render(ui(<CsvImportDialog onClose={() => undefined} />));
    choose("from,to,since\np1,c1,2020\np2,c9,2021\n", "jobs.csv");
    expect(await screen.findByText("2 rows · 3 columns (the first row is the header)", {}, READ)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "A relationship" }));

    // Rule-derived types are not offered; the declared pair is the default.
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Relationship type" })).toHaveValue("WORKS_AT"));
    expect(screen.queryByRole("option", { name: "DERIVED", hidden: true })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Source label" })).toHaveValue("Person");
    expect(screen.getByRole("textbox", { name: "Target label" })).toHaveValue("Company");
    expect(screen.getByRole("textbox", { name: "Source id column" })).toHaveValue("from");
    expect(screen.getByRole("textbox", { name: "Target id column" })).toHaveValue("to");
    expect(await screen.findByText("Nothing to report: every row reads as mapped.", {}, READ)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Import 2 rows" }));
    await waitFor(() => expect(FakeXhr.last).not.toBeNull());
    expect(FakeXhr.last!.url).toBe("/api/v1/acme/graph/edges:bulk");
    expect((await sentLines())[0]).toEqual({
      src_id: "p1",
      src_label: "Person",
      dst_id: "c1",
      dst_label: "Company",
      type: "WORKS_AT",
      properties: { since: 2020 },
    });
    act(() => FakeXhr.last!.answer(EDGES_RESULT));
    expect(
      await screen.findByText("1 relationship skipped: its source or target node does not exist", {}, READ),
    ).toBeInTheDocument();
  });

  it("reads the file again with another separator, and says when it has no rows", async () => {
    render(ui(<CsvImportDialog onClose={() => undefined} />));
    choose("a;b\n1;2\n3;4\n");
    expect(await screen.findByText("2 rows · 2 columns (the first row is the header)", {}, READ)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Semicolon" })).toBeChecked();
    fireEvent.click(screen.getByRole("radio", { name: "Comma" }));
    expect(await screen.findByText("2 rows · 1 columns (the first row is the header)", {}, READ)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Comma" })).toBeChecked();
    choose("only,a,header\n");
    expect(await screen.findByText("The file has no rows under its header row.", {}, READ)).toBeInTheDocument();
  });
});

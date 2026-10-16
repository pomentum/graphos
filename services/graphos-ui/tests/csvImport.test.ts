import type { EffectivePolicy, PropertyDefinitions } from "../src/api/types";
import { readFileText } from "../src/lib/csv";
import {
  INVALID,
  ID_NAMES,
  SOURCE_NAMES,
  TARGET_NAMES,
  cellValue,
  checkRows,
  checkRowsAsync,
  errorRow,
  guessColumn,
  planColumns,
  prepareImport,
  policyOf,
  propertyNameOf,
  recordOf,
  type EdgePlan,
  type Issue,
  type NodePlan,
} from "../src/lib/csvImport";

const PERSON: EffectivePolicy = {
  mode: "strict",
  undeclared: "warn",
  undeclared_source: "Person",
  properties: {
    name: { source: "Person", definition: { type: "string", required: true } },
    age: { source: "Person", definition: { type: "integer", minimum: 0, maximum: 150 } },
    status: { source: "*", definition: { type: "string", enum: ["active", "left"] } },
    member: { source: "Person", definition: { type: "boolean" } },
  },
};
const DEFINITIONS: PropertyDefinitions = {
  tenant: "acme",
  mode: "warn",
  vertex: { Person: PERSON, "*": { mode: "warn", undeclared: "allow", undeclared_source: "*", properties: {} } },
  edge: {},
};

function issue(issues: Issue[], code: Issue["code"]): Issue | undefined {
  return issues.find((found) => found.code === code);
}

describe("mapping CSV columns", () => {
  it("turns a header into a property name graphos accepts", () => {
    expect(propertyNameOf("name", 0)).toBe("name");
    expect(propertyNameOf(" First name ", 0)).toBe("First_name");
    expect(propertyNameOf("Prénom", 0)).toBe("Prenom");
    expect(propertyNameOf("2024 total (€)", 0)).toBe("_2024_total");
    expect(propertyNameOf("---", 2)).toBe("column_3");
    expect(propertyNameOf("x".repeat(80), 0)).toHaveLength(63);
  });

  it("finds the id columns by name, else by position", () => {
    expect(guessColumn(["name", "ID"], ID_NAMES, 0)).toBe(1);
    expect(guessColumn(["name", "other"], ID_NAMES, 0)).toBe(0);
    expect(guessColumn(["Source ID", "x", "target_id"], SOURCE_NAMES, 0)).toBe(0);
    expect(guessColumn(["Source ID", "x", "target_id"], TARGET_NAMES, 1)).toBe(2);
    expect(guessColumn(["only"], TARGET_NAMES, 1)).toBe(0);
  });

  it("offers every other column as a property, typed as declared, reserved names left out", () => {
    const header = ["id", "name", "age", "graphos_degree", "community_id", "member", "notes"];
    const columns = planColumns(header, [0], "nodes", PERSON, { 6: { property: "remarks", type: "text" } });
    expect(columns.map((c) => [c.header, c.property, c.type])).toEqual([
      ["name", "name", "text"],
      ["age", "age", "number"],
      ["graphos_degree", null, "auto"],
      ["community_id", null, "auto"],
      ["member", "member", "boolean"],
      ["notes", "remarks", "text"],
    ]);
    expect(policyOf(DEFINITIONS, "nodes", "Person")).toBe(PERSON);
    expect(policyOf(DEFINITIONS, "nodes", "Company")).toBe(DEFINITIONS.vertex["*"]);
    expect(policyOf(DEFINITIONS, "edges", "WORKS_AT")).toBeUndefined();
  });

  it("reads a cell as its column's type", () => {
    expect(cellValue("", "number")).toBeUndefined();
    expect(cellValue("42", "auto")).toBe(42);
    expect(cellValue("0042", "auto")).toBe("0042");
    expect(cellValue("true", "auto")).toBe(true);
    expect(cellValue("42", "text")).toBe("42");
    expect(cellValue(" 1.5 ", "number")).toBe(1.5);
    expect(cellValue("1,5", "number")).toBe(INVALID);
    expect(cellValue("Yes", "boolean")).toBe(true);
    expect(cellValue("0", "boolean")).toBe(false);
    expect(cellValue("maybe", "boolean")).toBe(INVALID);
  });

  it("makes nodes and edges as graphos takes them, empty and unreadable cells left out", () => {
    const node: NodePlan = {
      kind: "nodes",
      label: "Person",
      id: 0,
      columns: [
        { index: 1, header: "name", property: "name", type: "text" },
        { index: 2, header: "age", property: "age", type: "number" },
        { index: 3, header: "x", property: null, type: "auto" },
      ],
    };
    expect(recordOf(node, ["p1", "Ada", "n/a", "skip me"])).toEqual({
      id: "p1",
      label: "Person",
      properties: { name: "Ada" },
    });
    const edge: EdgePlan = {
      kind: "edges",
      type: "WORKS_AT",
      srcLabel: "Person",
      src: 0,
      dstLabel: "Company",
      dst: 1,
      columns: [{ index: 2, header: "since", property: "since", type: "auto" }],
    };
    expect(recordOf(edge, ["p1", "c1", "2020"])).toEqual({
      src_id: "p1",
      src_label: "Person",
      dst_id: "c1",
      dst_label: "Company",
      type: "WORKS_AT",
      properties: { since: 2020 },
    });
  });

  it("reads only plain decimal numbers, and no whole number past 2^53", () => {
    for (const text of ["42", "-7", "+3", "1.5", ".5", "2.", "1e3", "-2.5E-2"]) {
      expect(cellValue(text, "number")).toBe(Number(text));
    }
    for (const text of [
      "0x1F",
      "0b101",
      "0o17",
      "Infinity",
      "NaN",
      "1_000",
      "1.2.3",
      "9007199254740993",
      "-9007199254740993",
    ]) {
      expect(cellValue(text, "number")).toBe(INVALID);
    }
    expect(cellValue("9007199254740991", "number")).toBe(Number.MAX_SAFE_INTEGER);
    expect(cellValue("0x1F", "auto")).toBe("0x1F");
    expect(cellValue("12345678901234567890", "auto")).toBe("12345678901234567890");
  });

  it("keeps leading zeros as text and reads TRUE / FALSE in Auto", () => {
    expect(cellValue("007", "auto")).toBe("007");
    expect(cellValue("-01", "auto")).toBe("-01");
    expect(cellValue("0", "auto")).toBe(0);
    expect(cellValue("0.25", "auto")).toBe(0.25);
    expect(cellValue("TRUE", "auto")).toBe(true);
    expect(cellValue("False", "auto")).toBe(false);
    expect(cellValue("007", "number")).toBe(7);
  });

  it("takes off the apostrophe graphos's export puts before a formula-like cell", () => {
    expect(cellValue("'=SUM(A1)", "text")).toBe("=SUM(A1)");
    expect(cellValue("'-5", "auto")).toBe(-5);
    expect(cellValue("'+1", "number")).toBe(1);
    expect(cellValue("'@home", "auto")).toBe("@home");
    expect(cellValue("'quoted'", "text")).toBe("'quoted'");
  });

  it("trims ids, and a column named __proto__ is a property like the others", () => {
    const plan: NodePlan = {
      kind: "nodes",
      label: "Person",
      id: 0,
      columns: [{ index: 1, header: "__proto__", property: "__proto__", type: "text" }],
    };
    const record = recordOf(plan, ["  p1 ", "kept"]);
    expect(record).toMatchObject({ id: "p1" });
    expect(Object.keys(record.properties)).toEqual(["__proto__"]);
    expect(JSON.stringify(record)).toBe('{"id":"p1","label":"Person","properties":{"__proto__":"kept"}}');
  });
});

describe("preparing CSV rows for graphos", () => {
  const nodes: NodePlan = {
    kind: "nodes",
    label: "Person",
    id: 0,
    columns: [
      { index: 1, header: "name", property: "name", type: "text" },
      { index: 2, header: "age", property: "age", type: "number" },
    ],
  };

  async function linesOf(blob: Blob): Promise<unknown[]> {
    return (await readFileText(blob))
      .trimEnd()
      .split("\n")
      .map((line) => JSON.parse(line));
  }

  it("merges rows with the same id in file order: a later non-empty cell wins", async () => {
    const rows = [
      ["a", "Ada", "36"],
      [" ", "nobody", ""],
      ["b", "Bob", ""],
      [" a", "", "37"],
      ["a ", "Ada L.", "x"],
      ["", "nobody either", ""],
    ];
    const prepared = await prepareImport(nodes, rows);
    expect(prepared.lines).toBe(4);
    expect(prepared.rowOfLine).toEqual([1, 2, 3, 6]);
    expect(await linesOf(prepared.body)).toEqual([
      { id: "a", label: "Person", properties: { name: "Ada L.", age: 37 } },
      { id: "", label: "Person", properties: { name: "nobody" } },
      { id: "b", label: "Person", properties: { name: "Bob" } },
      { id: "", label: "Person", properties: { name: "nobody either" } },
    ]);
    expect(errorRow("line 4: id: String should have at least 1 character", prepared.rowOfLine)).toEqual({
      row: 6,
      message: "id: String should have at least 1 character",
    });
    expect(errorRow("line 9: past the end", prepared.rowOfLine)).toBeNull();
    expect(errorRow("stream error", prepared.rowOfLine)).toBeNull();
  });

  it("merges relationships with the same two ends", async () => {
    const edges: EdgePlan = {
      kind: "edges",
      type: "WORKS_AT",
      srcLabel: "Person",
      src: 0,
      dstLabel: "Company",
      dst: 1,
      columns: [{ index: 2, header: "role", property: "role", type: "text" }],
    };
    const prepared = await prepareImport(edges, [
      ["p1", "c1", "intern"],
      ["p1", "c2", "lead"],
      [" p1", "c1 ", "engineer"],
    ]);
    expect(prepared.rowOfLine).toEqual([1, 2]);
    const lines = (await linesOf(prepared.body)) as { dst_id: string; properties: unknown }[];
    expect(lines.map((line) => [line.dst_id, line.properties])).toEqual([
      ["c1", { role: "engineer" }],
      ["c2", { role: "lead" }],
    ]);
  });
});

describe("checking CSV rows", () => {
  const header = ["id", "name", "age", "status", "graphos_rank", "nick name", "colour"];
  const rows = [
    ["p1", "Ada", "36", "active", "", "", "red"],
    ["p2", "", "-1", "gone", "", "", "blue"],
    ["p1", "Bob", "x", "left", "", "", ""],
    ["", "Cy", "1.5", "active", "", ""],
  ];
  const plan: NodePlan = {
    kind: "nodes",
    label: "Person",
    id: 0,
    columns: [
      ...planColumns(header, [0], "nodes", PERSON, { 4: { property: "graphos_rank" }, 5: { property: "nick name" } }),
    ],
  };

  it("finds empty and repeated ids, unreadable values and ragged rows over every row", () => {
    const { rows: count, issues } = checkRows(plan, header, rows, PERSON);
    expect(count).toBe(4);
    expect(issue(issues, "emptyId")).toMatchObject({ count: 1, refused: true, examples: [{ row: 4, column: "id" }] });
    expect(issue(issues, "duplicateId")).toMatchObject({
      count: 1,
      refused: false,
      examples: [{ row: 3, value: "p1" }],
    });
    expect(issue(issues, "badValue")).toMatchObject({ count: 1, examples: [{ row: 3, column: "age", value: "x" }] });
    expect(issue(issues, "ragged")).toMatchObject({ count: 1, examples: [{ row: 4 }] });
  });

  it("names the property names graphos refuses or that are not valid", () => {
    const { issues } = checkRows(plan, header, rows, PERSON);
    expect(issue(issues, "reservedName")).toMatchObject({
      count: 1,
      refused: true,
      examples: [{ column: "graphos_rank" }],
    });
    expect(issue(issues, "invalidName")).toMatchObject({ examples: [{ column: "nick name" }] });
    expect(issue(issues, "undeclared")?.examples.map((e) => e.column)).toEqual(["nick name", "colour"]);
  });

  it("checks the declared definitions: required, type, allowed values and range, refused when strict", () => {
    const { issues } = checkRows(plan, header, rows, PERSON);
    expect(issue(issues, "required")).toMatchObject({
      count: 1,
      refused: true,
      examples: [{ row: 2, column: "name" }],
    });
    expect(issue(issues, "notInEnum")).toMatchObject({ count: 1, examples: [{ row: 2, value: "gone" }] });
    expect(issue(issues, "outOfRange")).toMatchObject({ count: 1, examples: [{ row: 2, value: "-1" }] });
    expect(issue(issues, "wrongType")).toMatchObject({ count: 1, examples: [{ row: 4, column: "age", value: "1.5" }] });
    expect(issues.findIndex((found) => !found.refused)).toBeGreaterThan(issues.findIndex((found) => found.refused));

    const warned = checkRows(plan, header, rows, { ...PERSON, mode: "warn" }).issues;
    expect(issue(warned, "required")?.refused).toBe(false);
    const off = checkRows(plan, header, rows, { ...PERSON, mode: "off" }).issues;
    expect(issue(off, "required")).toBeUndefined();
    expect(issue(off, "notInEnum")).toBeUndefined();
  });

  it("checks required properties on the merged node: one row of a repeated id is enough", () => {
    const named: NodePlan = {
      kind: "nodes",
      label: "Person",
      id: 0,
      columns: [{ index: 1, header: "name", property: "name", type: "text" }],
    };
    const rows = [
      ["p1", "Ann"],
      ["p1", ""],
      ["p2", ""],
      ["p2", ""],
      ["", ""],
    ];
    const { issues } = checkRows(named, ["id", "name"], rows, PERSON);
    expect(issue(issues, "required")).toMatchObject({
      count: 2,
      examples: [
        { row: 5, column: "name" },
        { row: 3, column: "name" },
      ],
    });
  });

  it("refuses whitespace-only ids and reports an imported column without a name", () => {
    const plan: NodePlan = {
      kind: "nodes",
      label: "Person",
      id: 0,
      columns: [{ index: 1, header: "Nick", property: "", type: "text" }],
    };
    const { issues } = checkRows(
      plan,
      ["id", "Nick"],
      [
        ["   ", "x"],
        [" p1 ", "y"],
        ["p1", "z"],
      ],
      undefined,
    );
    expect(issue(issues, "emptyId")).toMatchObject({ count: 1, refused: true, examples: [{ row: 1 }] });
    expect(issue(issues, "duplicateId")).toMatchObject({ count: 1, examples: [{ row: 3, value: "p1" }] });
    expect(issue(issues, "emptyName")).toMatchObject({ count: 1, examples: [{ column: "Nick" }] });
    expect(issue(issues, "invalidName")).toBeUndefined();
  });

  it("checks a slice at a time to the same result, and a stale check stops", async () => {
    const many = Array.from({ length: 250 }, (_, at) => rows[at % rows.length]!);
    const shares: number[] = [];
    const slow = await checkRowsAsync(plan, header, many, PERSON, undefined, {
      slice: 40,
      onProgress: (share) => shares.push(share),
    });
    expect(slow).toEqual(checkRows(plan, header, many, PERSON));
    expect(shares).toHaveLength(7);
    expect(shares.at(-1)).toBe(1);
    const controller = new AbortController();
    const stale = checkRowsAsync(plan, header, many, PERSON, undefined, { slice: 40, signal: controller.signal });
    controller.abort();
    await expect(stale).rejects.toThrow();
  });

  it("checks a relationship's ids and its label pair against the declared ones", () => {
    const edge: EdgePlan = {
      kind: "edges",
      type: "WORKS_AT",
      srcLabel: "Company",
      src: 0,
      dstLabel: "Person",
      dst: 1,
      columns: [],
    };
    const schema = {
      edge_endpoints: { WORKS_AT: [{ src: "Person", dst: "Company" }] },
      label_hierarchy: {},
      strict_endpoints: true,
    };
    const { issues } = checkRows(
      edge,
      ["from", "to"],
      [
        ["c1", ""],
        ["c1", "p1"],
      ],
      undefined,
      schema,
    );
    expect(issue(issues, "emptyId")).toMatchObject({ count: 1, examples: [{ row: 1, column: "to" }] });
    expect(issue(issues, "duplicateId")).toBeUndefined();
    const repeated = checkRows(
      edge,
      ["from", "to"],
      [
        ["c1", "p1"],
        ["c1 ", " p1"],
      ],
      undefined,
    );
    expect(issue(repeated.issues, "duplicatePair")).toMatchObject({
      count: 1,
      examples: [{ row: 2, value: "c1 → p1" }],
    });
    expect(issue(issues, "undeclaredPair")).toMatchObject({
      refused: true,
      examples: [{ value: "Company → WORKS_AT → Person" }],
    });
    const fine = checkRows(
      { ...edge, srcLabel: "Person", dstLabel: "Company" },
      ["from", "to"],
      [["p1", "c1"]],
      undefined,
      schema,
    );
    expect(fine.issues).toEqual([]);
  });
});

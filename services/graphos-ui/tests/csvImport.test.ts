import type { EffectivePolicy, PropertyDefinitions } from "../src/api/types";
import {
  INVALID,
  ID_NAMES,
  SOURCE_NAMES,
  TARGET_NAMES,
  cellValue,
  guessColumn,
  headTable,
  idOf,
  importQuery,
  issueMessage,
  planColumns,
  policyOf,
  propertyNameOf,
  type EdgePlan,
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

  it("reads an id without the spaces around it or the export's formula guard", () => {
    expect(idOf(["  p1 ", "x"], 0)).toBe("p1");
    expect(idOf(["'-7"], 0)).toBe("-7");
    expect(idOf(["a"], 3)).toBe("");
  });
});

describe("the CSV import query", () => {
  const header = ["key", "name", "Age", "graphos_rank"];

  it("names the id column by its header and every other column with its property and type", () => {
    const plan: NodePlan = {
      kind: "nodes",
      label: "Person",
      id: 0,
      columns: planColumns(header, [0], "nodes", PERSON, { 2: { property: "age" } }),
    };
    const query = importQuery(plan, header, ";");
    expect(query).toEqual({ label: "Person", id: "key", delimiter: "semicolon", columns: expect.any(String) });
    expect(JSON.parse(query.columns)).toEqual({
      name: { property: "name", type: "text" },
      Age: { property: "age", type: "number" },
      graphos_rank: { property: null, type: "auto" },
    });
  });

  it("names a relationship's type, labels and end columns", () => {
    const plan: EdgePlan = {
      kind: "edges",
      type: "WORKS_AT",
      srcLabel: "Person",
      src: 1,
      dstLabel: "Company",
      dst: 0,
      columns: [],
    };
    expect(importQuery(plan, ["to", "from"], "\t")).toEqual({
      type: "WORKS_AT",
      src_label: "Person",
      dst_label: "Company",
      src: "from",
      dst: "to",
      columns: "{}",
      delimiter: "tab",
    });
  });

  it("reads the start of a large file without its last, maybe cut, row", () => {
    expect(headTable("a,b\n1,2\n3,4", true).rows).toEqual([
      ["1", "2"],
      ["3", "4"],
    ]);
    const start = headTable('a;b\n1;2\n3;"cut', false);
    expect(start).toMatchObject({ delimiter: ";", header: ["a", "b"], rows: [["1", "2"]], openQuote: false });
    expect(headTable("a,b\n1;2\n", true, ";").header).toEqual(["a,b"]);
  });

  it("names graphos's issue codes by their message, an unknown one by none", () => {
    expect(issueMessage("empty_id")).toBe("emptyId");
    expect(issueMessage("duplicate_pair")).toBe("duplicatePair");
    expect(issueMessage("undeclared_pair")).toBe("undeclaredPair");
    expect(issueMessage("something_new")).toBeNull();
    expect(issueMessage("constructor")).toBeNull();
  });
});

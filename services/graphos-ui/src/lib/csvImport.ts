import type {
  CsvColumnType,
  CsvDelimiterName,
  CsvImportQuery,
  EffectivePolicy,
  PropertyDefinitions,
  PropertySpec,
} from "../api/types";
import { parseCsv, type CsvDelimiter, type CsvTable } from "./csv";
import { RESERVED_PREFIX } from "./properties";
import { PROPERTY_NAME } from "./propertySchema";

/**
 * A CSV file mapped onto the graph: each row one node of a label, or one
 * relationship of a type between two labelled nodes; its other columns
 * properties of a chosen type, or left out. graphos reads the file itself
 * (`nodes:csv` / `edges:csv`), merges and checks its rows; the page reads
 * only its start, for the header and a preview, with the same defaults and
 * value rules (`graphos.csv_rows`).
 */

export type RowKind = "nodes" | "edges";
export type ColumnType = CsvColumnType;
export const COLUMN_TYPES: readonly ColumnType[] = ["auto", "text", "number", "boolean"];

/** What one column becomes: a property of a type, or nothing (`property: null`). */
export interface ColumnChoice {
  property: string | null;
  type: ColumnType;
}

export interface ColumnPlan extends ColumnChoice {
  index: number;
  header: string;
}

export interface NodePlan {
  kind: "nodes";
  label: string;
  /** The column holding each node's id. */
  id: number;
  columns: ColumnPlan[];
}

export interface EdgePlan {
  kind: "edges";
  type: string;
  srcLabel: string;
  src: number;
  dstLabel: string;
  dst: number;
  columns: ColumnPlan[];
}

export type ImportPlan = NodePlan | EdgePlan;

/** Names graphos refuses in a write; for nodes also `id`, set from the id column. */
export function isReservedName(name: string, kind: RowKind): boolean {
  return name.startsWith(RESERVED_PREFIX) || name === "community_id" || (kind === "nodes" && name === "id");
}

/** A column's header as a property name graphos accepts: `First name` → `First_name`, `2024` → `_2024`. */
export function propertyNameOf(header: string, index: number): string {
  const trimmed = header.trim();
  if (PROPERTY_NAME.test(trimmed)) return trimmed;
  const name = trimmed
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^A-Za-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (!name) return `column_${index + 1}`;
  return (/^[0-9]/.test(name) ? `_${name}` : name).slice(0, 63);
}

/** The definitions a label (nodes) or type (edges) is checked against. */
export function policyOf(
  definitions: PropertyDefinitions | undefined,
  kind: RowKind,
  name: string | null,
): EffectivePolicy | undefined {
  const part = kind === "nodes" ? definitions?.vertex : definitions?.edge;
  return (name ? part?.[name] : undefined) ?? part?.["*"];
}

/** A property's declared definition (own entries only: `constructor` or `__proto__` are names like others). */
export function specOf(policy: EffectivePolicy | undefined, name: string): PropertySpec | undefined {
  return policy && Object.hasOwn(policy.properties, name) ? policy.properties[name]!.definition : undefined;
}

/** The column type a declared property suggests; `auto` when it declares none that a cell can hold. */
export function typeFor(spec: PropertySpec | undefined): ColumnType {
  switch (spec?.type) {
    case "string":
      return "text";
    case "number":
    case "integer":
      return "number";
    case "boolean":
      return "boolean";
    default:
      return "auto";
  }
}

/**
 * Every column but the key ones (ids), as properties: the user's choices
 * where made, else the header as a name (reserved names left out) of the
 * type its definition declares.
 */
export function planColumns(
  header: string[],
  keys: number[],
  kind: RowKind,
  policy: EffectivePolicy | undefined,
  choices: Record<number, Partial<ColumnChoice>>,
): ColumnPlan[] {
  return header.flatMap((title, index) => {
    if (keys.includes(index)) return [];
    const choice = choices[index] ?? {};
    const suggested = propertyNameOf(title, index);
    const property =
      choice.property !== undefined ? choice.property : isReservedName(suggested, kind) ? null : suggested;
    const type = choice.type ?? typeFor(property ? specOf(policy, property) : undefined);
    return [{ index, header: title, property, type }];
  });
}

/** The column a header names (case and `_` ignored), else `fallback` when the table has it. */
export function guessColumn(header: string[], names: string[], fallback: number): number {
  const wanted = new Set(names);
  const found = header.findIndex((title) =>
    wanted.has(
      title
        .trim()
        .toLowerCase()
        .replace(/[\s_-]+/g, ""),
    ),
  );
  return found >= 0 ? found : Math.min(fallback, Math.max(0, header.length - 1));
}

export const ID_NAMES = ["id", "key", "identifier"];
export const SOURCE_NAMES = ["src", "srcid", "source", "sourceid", "from", "fromid", "start", "startid"];
export const TARGET_NAMES = ["dst", "dstid", "target", "targetid", "to", "toid", "end", "endid"];

/** What a cell that does not read as its column's type becomes. */
export const INVALID = Symbol("invalid");

/** A plain decimal number: an optional sign, digits, an optional fraction and exponent (no `0x`, no `Infinity`). */
const DECIMAL = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;
/** A number as Auto reads it: no sign but `-`, no leading zero (`007` stays text), no exponent. */
const AUTO_NUMBER = /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/;
/** A whole number written without a fraction or an exponent. */
const WHOLE = /^[+-]?\d+$/;
/** graphos's CSV export puts `'` before a cell starting like a formula; it is taken off again. */
const FORMULA_GUARD = /^'[=+\-@\t\r]/;

/** A cell's text as written before graphos's export guarded it against spreadsheet formulas. */
export function unguarded(text: string): string {
  return FORMULA_GUARD.test(text) ? text.slice(1) : text;
}

/** A number, or `INVALID` when the text is not a plain decimal or is a whole number past 2^53. */
function numberOf(text: string, pattern: RegExp): number | typeof INVALID {
  if (!pattern.test(text)) return INVALID;
  const value = Number(text);
  if (!Number.isFinite(value)) return INVALID;
  if (WHOLE.test(text) && !Number.isSafeInteger(value)) return INVALID;
  return value;
}

/**
 * A cell as a value of the column's type, `undefined` when empty (the
 * property is left out), `INVALID` when it does not read as the type.
 * `auto` reads `42`, `-1.5`, `TRUE` as a number or a boolean, else text
 * (`007`, `1e3` and whole numbers past 2^53 stay text).
 */
export function cellValue(text: string | undefined, type: ColumnType): unknown {
  if (text === undefined || text === "") return undefined;
  const cell = unguarded(text);
  switch (type) {
    case "text":
      return cell;
    case "auto": {
      const word = cell.toLowerCase();
      if (word === "true" || word === "false") return word === "true";
      const value = numberOf(cell, AUTO_NUMBER);
      return value === INVALID ? cell : value;
    }
    case "number":
      return numberOf(cell.trim(), DECIMAL);
    case "boolean": {
      const word = cell.trim().toLowerCase();
      if (word === "true" || word === "yes" || word === "1") return true;
      if (word === "false" || word === "no" || word === "0") return false;
      return INVALID;
    }
  }
}

/** A row's id in a column, without the spaces around it ("" when there is none). */
export function idOf(row: string[], column: number): string {
  return unguarded(row[column] ?? "").trim();
}

/** The start of a large file read for the header and the preview. */
export const HEAD_BYTES = 1024 * 1024;

/**
 * The start of a file as a table. When it is not the whole file, its last
 * row (maybe cut in the middle) is dropped, and an open quote is not
 * reported (graphos, reading all of it, says).
 */
export function headTable(text: string, whole: boolean, delimiter?: CsvDelimiter): CsvTable {
  const table = parseCsv(text, delimiter);
  if (whole) return table;
  return { ...table, rows: table.rows.slice(0, -1), openQuote: false };
}

export const DELIMITER_NAMES: Record<CsvDelimiter, CsvDelimiterName> = { ",": "comma", ";": "semicolon", "\t": "tab" };

/**
 * The query graphos reads a file with as the plan says: the label or type,
 * the id columns by their header, and every other column named with its
 * property (`null`: left out) and type, so graphos maps it as shown.
 */
export function importQuery(plan: ImportPlan, header: string[], delimiter: CsvDelimiter): CsvImportQuery {
  const columns: Record<string, ColumnChoice> = {};
  for (const column of plan.columns) columns[column.header] = { property: column.property, type: column.type };
  const shared = { columns: JSON.stringify(columns), delimiter: DELIMITER_NAMES[delimiter] };
  if (plan.kind === "nodes") return { label: plan.label, id: header[plan.id] ?? "", ...shared };
  return {
    type: plan.type,
    src_label: plan.srcLabel,
    dst_label: plan.dstLabel,
    src: header[plan.src] ?? "",
    dst: header[plan.dst] ?? "",
    ...shared,
  };
}

/** The issues graphos reports on a CSV file, by the name of their message. */
const ISSUE_MESSAGES = {
  empty_id: "emptyId",
  long_id: "longId",
  ragged: "ragged",
  bad_value: "badValue",
  duplicate_id: "duplicateId",
  duplicate_pair: "duplicatePair",
  undeclared_pair: "undeclaredPair",
} as const;

export type IssueMessage = (typeof ISSUE_MESSAGES)[keyof typeof ISSUE_MESSAGES];

/** The message for an issue graphos reports; null for one this page does not know (shown by its code). */
export function issueMessage(code: string): IssueMessage | null {
  return Object.hasOwn(ISSUE_MESSAGES, code) ? ISSUE_MESSAGES[code as keyof typeof ISSUE_MESSAGES] : null;
}

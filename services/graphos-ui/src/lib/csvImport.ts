import type {
  EdgeUpsert,
  EffectivePolicy,
  NodeUpsert,
  PropertyDefinitions,
  PropertySpec,
  TenantSchema,
} from "../api/types";
import { pause } from "./csv";
import { Endpoints } from "./endpoints";
import { RESERVED_PREFIX } from "./properties";
import { PROPERTY_NAME } from "./propertySchema";

/**
 * A CSV file mapped onto the graph: each row one node of a label, or one
 * relationship of a type between two labelled nodes; its other columns
 * properties of a chosen type, or left out. The checks run over every row
 * before anything is sent, as graphos would (it checks again).
 */

export type RowKind = "nodes" | "edges";
export type ColumnType = "auto" | "text" | "number" | "boolean";
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

/** The longest node id graphos takes (`contracts.graph`). */
export const MAX_ID_LENGTH = 512;

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

/** Properties without a prototype, so a column named `__proto__` is a property like the others. */
function propertiesOf(columns: ColumnPlan[], row: string[]): Record<string, unknown> {
  const properties = Object.create(null) as Record<string, unknown>;
  for (const column of columns) {
    if (!column.property) continue;
    const value = cellValue(row[column.index], column.type);
    if (value !== undefined && value !== INVALID) properties[column.property] = value;
  }
  return properties;
}

/** A row's id in a column, without the spaces around it ("" when there is none). */
export function idOf(row: string[], column: number): string {
  return unguarded(row[column] ?? "").trim();
}

/** One row as graphos takes it (`nodes:bulk` / `edges:bulk`); cells that do not convert are left out. */
export function recordOf(plan: ImportPlan, row: string[]): NodeUpsert | EdgeUpsert {
  const properties = propertiesOf(plan.columns, row);
  if (plan.kind === "nodes") return { id: idOf(row, plan.id), label: plan.label, properties };
  return {
    src_id: idOf(row, plan.src),
    src_label: plan.srcLabel,
    dst_id: idOf(row, plan.dst),
    dst_label: plan.dstLabel,
    type: plan.type,
    properties,
  };
}

/** What rows with the same key become one of: a node's id, a relationship's two ends; null without one. */
function keyOf(record: NodeUpsert | EdgeUpsert): string | null {
  if ("id" in record) return record.id || null;
  return record.src_id && record.dst_id ? JSON.stringify([record.src_id, record.dst_id]) : null;
}

/** Rows handled between two pauses that let the page draw. */
const ROWS_PER_SLICE = 20_000;

/** The rows as graphos is sent them: one NDJSON line per node or relationship. */
export interface PreparedImport {
  body: Blob;
  lines: number;
  /** Per line sent (from 0), the file's row (from 1) it comes from: the first of the rows merged into it. */
  rowOfLine: number[];
}

/**
 * Every row as NDJSON, rows with the same id (nodes) or the same two ends
 * (relationships) merged into one line, in the file's order: a later
 * non-empty cell wins, an empty or unreadable one keeps the earlier value
 * (graphos itself would keep only one of them within a batch it writes).
 * Rows without an id are sent as they are, for graphos to refuse.
 */
export async function prepareImport(plan: ImportPlan, rows: string[][], signal?: AbortSignal): Promise<PreparedImport> {
  const records: (NodeUpsert | EdgeUpsert)[] = [];
  const rowOfLine: number[] = [];
  const lineOf = new Map<string, number>();
  for (let start = 0; start < rows.length; start += ROWS_PER_SLICE) {
    signal?.throwIfAborted();
    const end = Math.min(rows.length, start + ROWS_PER_SLICE);
    for (let at = start; at < end; at += 1) {
      const record = recordOf(plan, rows[at]!);
      const key = keyOf(record);
      const line = key === null ? undefined : lineOf.get(key);
      if (line === undefined) {
        if (key !== null) lineOf.set(key, records.length);
        records.push(record);
        rowOfLine.push(at + 1);
      } else {
        Object.assign(records[line]!.properties, record.properties);
      }
    }
    if (end < rows.length) await pause();
  }
  const parts: string[] = [];
  for (let start = 0; start < records.length; start += ROWS_PER_SLICE) {
    signal?.throwIfAborted();
    let text = "";
    for (const record of records.slice(start, start + ROWS_PER_SLICE)) text += `${JSON.stringify(record)}\n`;
    parts.push(text);
    if (start + ROWS_PER_SLICE < records.length) await pause();
  }
  return { body: new Blob(parts, { type: "application/x-ndjson" }), lines: records.length, rowOfLine };
}

/** graphos's `line N: …` as the file's row and the message; null when it names no line sent. */
export function errorRow(error: string, rowOfLine: number[]): { row: number; message: string } | null {
  const match = /^line (\d+): ([\s\S]*)$/.exec(error);
  const row = match ? rowOfLine[Number(match[1]) - 1] : undefined;
  return match && row !== undefined ? { row, message: match[2]! } : null;
}

/** What the checks find. Column checks count columns; row checks count rows or cells. */
export type IssueCode =
  | "emptyId"
  | "longId"
  | "duplicateId"
  | "duplicatePair"
  | "ragged"
  | "badValue"
  | "reservedName"
  | "emptyName"
  | "invalidName"
  | "sameName"
  | "undeclared"
  | "required"
  | "wrongType"
  | "notInEnum"
  | "outOfRange"
  | "undeclaredPair";

export interface IssueExample {
  /** The row in the file, the header not counted. */
  row?: number;
  column?: string;
  value?: string;
}

export interface Issue {
  code: IssueCode;
  count: number;
  examples: IssueExample[];
  /** graphos refuses such rows (or the whole row's write); otherwise it writes them, maybe with a warning. */
  refused: boolean;
}

export interface ImportCheck {
  rows: number;
  issues: Issue[];
}

/** Examples kept per issue. */
export const EXAMPLES = 3;
/** Characters of a value shown in an example. */
const SHOWN = 40;

class Issues {
  private readonly found = new Map<IssueCode, Issue>();

  add(code: IssueCode, refused: boolean, example: IssueExample): void {
    let issue = this.found.get(code);
    if (!issue) {
      issue = { code, count: 0, examples: [], refused };
      this.found.set(code, issue);
    }
    issue.count += 1;
    if (issue.examples.length < EXAMPLES) {
      issue.examples.push(
        example.value !== undefined && example.value.length > SHOWN
          ? { ...example, value: `${example.value.slice(0, SHOWN - 1)}…` }
          : example,
      );
    }
  }

  list(): Issue[] {
    return [...this.found.values()].sort((a, b) => Number(b.refused) - Number(a.refused));
  }
}

function breaks(spec: PropertySpec, value: unknown): "wrongType" | "notInEnum" | "outOfRange" | null {
  const type = spec.type;
  if (type && type !== "any") {
    const fits =
      type === "string"
        ? typeof value === "string"
        : type === "boolean"
          ? typeof value === "boolean"
          : type === "number"
            ? typeof value === "number"
            : type === "integer"
              ? Number.isInteger(value)
              : false;
    if (!fits) return "wrongType";
  }
  if (spec.enum?.length && !spec.enum.some((allowed) => allowed === value)) return "notInEnum";
  if (typeof value === "number") {
    if (spec.minimum !== undefined && value < spec.minimum) return "outOfRange";
    if (spec.maximum !== undefined && value > spec.maximum) return "outOfRange";
  }
  return null;
}

type EndpointSchema = Pick<TenantSchema, "edge_endpoints" | "label_hierarchy" | "strict_endpoints">;

/**
 * The checks over the rows, fed a slice of rows at a time: ids (empty, too
 * long, repeated), cells that do not read as their column's type, property
 * names graphos refuses or that are empty, and, unless the definitions are
 * off, the declared properties (required, type, allowed values, range;
 * patterns and formats are left to graphos). For relationships, the label
 * pair against the declared ones.
 */
class RowChecker {
  private readonly issues = new Issues();
  private readonly strict: boolean;
  private readonly mapped: ColumnPlan[];
  private readonly specs: (PropertySpec | undefined)[];
  private readonly required: string[];
  private readonly ids: number[];
  private readonly seen = new Set<string>();

  constructor(
    private readonly plan: ImportPlan,
    private readonly header: string[],
    policy: EffectivePolicy | undefined,
    schema: EndpointSchema | undefined,
  ) {
    const issues = this.issues;
    this.strict = policy?.mode === "strict";
    const checked = policy && policy.mode !== "off" ? policy : undefined;
    const included = plan.columns.filter((column) => column.property !== null);
    this.mapped = included.filter((column) => column.property);

    const named = new Set<string>();
    for (const column of included) {
      const name = column.property!;
      if (!name) {
        issues.add("emptyName", false, { column: column.header });
        continue;
      }
      if (isReservedName(name, plan.kind)) issues.add("reservedName", true, { column: name });
      else if (!PROPERTY_NAME.test(name)) issues.add("invalidName", false, { column: name });
      if (named.has(name)) issues.add("sameName", false, { column: name });
      named.add(name);
      const declared = checked ? Object.hasOwn(checked.properties, name) : true;
      if (checked && checked.undeclared !== "allow" && !declared && !isReservedName(name, plan.kind)) {
        issues.add("undeclared", this.strict && checked.undeclared === "reject", { column: name });
      }
    }
    if (plan.kind === "edges" && schema) {
      const coverage = new Endpoints(schema).check(plan.type, plan.srcLabel, plan.dstLabel);
      if (coverage.status === "undeclared") {
        issues.add("undeclaredPair", Boolean(schema.strict_endpoints), {
          value: `${plan.srcLabel} → ${plan.type} → ${plan.dstLabel}`,
        });
      }
    }
    this.specs = this.mapped.map((column) => (checked ? specOf(checked, column.property!) : undefined));
    this.required = checked
      ? Object.entries(checked.properties)
          .filter(([, entry]) => entry.definition.required)
          .map(([name]) => name)
      : [];
    this.ids = plan.kind === "nodes" ? [plan.id] : [plan.src, plan.dst];
  }

  /** Checks the file's rows from `start` up to `end` (not included). */
  step(rows: string[][], start: number, end: number): void {
    const { issues, header, ids, mapped, specs, required, strict } = this;
    for (let at = start; at < end; at += 1) {
      const row = rows[at]!;
      const line = at + 1;
      if (row.length !== header.length) issues.add("ragged", false, { row: line });
      const keys: string[] = [];
      for (const index of ids) {
        const id = idOf(row, index);
        keys.push(id);
        if (id === "") issues.add("emptyId", true, { row: line, column: header[index] });
        else if (id.length > MAX_ID_LENGTH) issues.add("longId", true, { row: line, column: header[index] });
      }
      if (keys.every(Boolean)) {
        const key = keys.length === 1 ? keys[0]! : JSON.stringify(keys);
        if (this.seen.has(key)) {
          const code = this.plan.kind === "nodes" ? "duplicateId" : "duplicatePair";
          issues.add(code, false, { row: line, value: keys.join(" → ") });
        }
        this.seen.add(key);
      }
      const present = required.length ? new Set<string>() : null;
      mapped.forEach((column, index) => {
        const text = row[column.index];
        const value = cellValue(text, column.type);
        if (value === undefined) return;
        const name = column.property!;
        if (value === INVALID) {
          issues.add("badValue", false, { row: line, column: name, value: text });
          return;
        }
        present?.add(name);
        const spec = specs[index];
        const broken = spec ? breaks(spec, value) : null;
        if (broken) issues.add(broken, strict, { row: line, column: name, value: text });
      });
      for (const name of required) {
        if (!present!.has(name)) issues.add("required", strict, { row: line, column: name });
      }
    }
  }

  result(rows: number): ImportCheck {
    return { rows, issues: this.issues.list() };
  }
}

/** Checks every row at once (see `checkRowsAsync`). */
export function checkRows(
  plan: ImportPlan,
  header: string[],
  rows: string[][],
  policy: EffectivePolicy | undefined,
  schema?: EndpointSchema,
): ImportCheck {
  const checker = new RowChecker(plan, header, policy, schema);
  checker.step(rows, 0, rows.length);
  return checker.result(rows.length);
}

export interface CheckOptions {
  /** Rows checked between two pauses that let the page draw. */
  slice?: number;
  /** How many rows are checked, from 0 to 1. */
  onProgress?: (share: number) => void;
  /** Stops a check made stale (the mapping changed): it rejects with the abort reason. */
  signal?: AbortSignal;
}

/** Checks every row as graphos would before writing, a slice at a time so the page stays responsive. */
export async function checkRowsAsync(
  plan: ImportPlan,
  header: string[],
  rows: string[][],
  policy: EffectivePolicy | undefined,
  schema?: EndpointSchema,
  options: CheckOptions = {},
): Promise<ImportCheck> {
  const slice = options.slice ?? ROWS_PER_SLICE;
  const checker = new RowChecker(plan, header, policy, schema);
  for (let start = 0; start < rows.length; start += slice) {
    options.signal?.throwIfAborted();
    const end = Math.min(rows.length, start + slice);
    checker.step(rows, start, end);
    options.onProgress?.(end / rows.length);
    if (end < rows.length) await pause();
  }
  options.signal?.throwIfAborted();
  return checker.result(rows.length);
}

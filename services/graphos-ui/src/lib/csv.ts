/**
 * CSV for spreadsheets: comma-separated, every cell quoted when it needs it,
 * UTF-8 with a byte-order mark so Excel reads accents and scripts right.
 *
 * A cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return is
 * prefixed with `'`: a spreadsheet would otherwise run it as a formula
 * (CSV injection). Lists and objects are written as JSON.
 */

export const CSV_TYPE = "text/csv;charset=utf-8";
const BOM = "\uFEFF";
const FORMULA_START = /^[=+\-@\t\r]/;

/** One cell as written in the file. */
export function csvCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  let text = typeof value === "object" ? JSON.stringify(value) : String(value);
  if (typeof value === "string" && FORMULA_START.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** One line of cells, ended by CRLF (RFC 4180). */
export function csvLine(cells: unknown[]): string {
  return `${cells.map(csvCell).join(",")}\r\n`;
}

/** A whole table as CSV text, BOM first. */
export function toCsv(header: string[], rows: unknown[][]): string {
  return BOM + csvLine(header) + rows.map(csvLine).join("");
}

/** The BOM and the header line, to start a streamed CSV. */
export function csvStart(header: string[]): string {
  return BOM + csvLine(header);
}

/** A file name safe on every system, from parts: `acme-Person-2026-10-14.csv`. */
export function exportName(parts: (string | null | undefined)[], extension: string): string {
  const day = new Date().toISOString().slice(0, 10);
  const base = [...parts.filter(Boolean), day].join("-").replace(/[^A-Za-z0-9_.-]+/g, "_");
  return `${base}.${extension}`;
}

/** The delimiters a CSV file is read with: the first that splits its lines evenly. */
export type CsvDelimiter = "," | ";" | "\t";
export const CSV_DELIMITERS: readonly CsvDelimiter[] = [",", ";", "\t"];

/** A CSV file read: its header row, the rows after it, and how it was split. */
export interface CsvTable {
  delimiter: CsvDelimiter;
  header: string[];
  rows: string[][];
  /** The file ended inside a quoted cell (a quote never closed): its last cell runs to the end. */
  openQuote: boolean;
}

const QUOTE = 34;
const LF = 10;
const CR = 13;

/**
 * An RFC 4180 reader fed text in pieces of any size: quoted cells (with `""`
 * for a quote, delimiters and line breaks inside), CRLF or LF line ends, a
 * byte-order mark at the start. A blank line is no row. Lenient where the
 * RFC is not: a quote inside an unquoted cell, or text after a closing
 * quote, is kept as written.
 */
export class CsvParser {
  readonly rows: string[][] = [];
  private readonly delimiter: number;
  private row: string[] = [];
  private cell = "";
  private cellQuoted = false;
  private inQuotes = false;
  /** A quote ended the last piece inside a quoted cell: an escaped quote or the closing one. */
  private quoteAtEnd = false;
  /** A CR ended the last piece: an LF starting the next one belongs to it. */
  private crAtEnd = false;
  private started = false;

  constructor(delimiter: CsvDelimiter) {
    this.delimiter = delimiter.charCodeAt(0);
  }

  /** Inside a quoted cell (after `end`: the text ended in one). */
  get openQuote(): boolean {
    return this.inQuotes && !this.quoteAtEnd;
  }

  push(text: string): void {
    let i = 0;
    const n = text.length;
    if (!this.started && n > 0) {
      this.started = true;
      if (text.charCodeAt(0) === 0xfeff) i = 1;
    }
    if (this.crAtEnd && i < n) {
      this.crAtEnd = false;
      if (text.charCodeAt(i) === LF) i += 1;
    }
    if (this.quoteAtEnd && i < n) {
      this.quoteAtEnd = false;
      if (text.charCodeAt(i) === QUOTE) {
        this.cell += '"';
        i += 1;
      } else {
        this.inQuotes = false;
      }
    }
    while (i < n) {
      if (this.inQuotes) {
        const quote = text.indexOf('"', i);
        if (quote < 0) {
          this.cell += text.slice(i);
          return;
        }
        this.cell += text.slice(i, quote);
        if (quote + 1 >= n) {
          this.quoteAtEnd = true;
          return;
        }
        if (text.charCodeAt(quote + 1) === QUOTE) {
          this.cell += '"';
          i = quote + 2;
        } else {
          this.inQuotes = false;
          i = quote + 1;
        }
        continue;
      }
      let j = i;
      let c = 0;
      while (j < n) {
        c = text.charCodeAt(j);
        if (c === this.delimiter || c === LF || c === CR || c === QUOTE) break;
        j += 1;
      }
      if (j > i) this.cell += text.slice(i, j);
      if (j >= n) return;
      i = j + 1;
      if (c === QUOTE) {
        if (this.cell === "" && !this.cellQuoted) {
          this.cellQuoted = true;
          this.inQuotes = true;
        } else {
          this.cell += '"';
        }
      } else if (c === this.delimiter) {
        this.endCell();
      } else {
        this.endRow();
        if (c === CR) {
          if (i >= n) this.crAtEnd = true;
          else if (text.charCodeAt(i) === LF) i += 1;
        }
      }
    }
  }

  /** Ends the text: the last row, without a line break after it, is kept. */
  end(): string[][] {
    if (this.quoteAtEnd) {
      this.quoteAtEnd = false;
      this.inQuotes = false;
    }
    if (this.cell !== "" || this.cellQuoted || this.row.length) this.endRow();
    return this.rows;
  }

  private endCell(): void {
    this.row.push(this.cell);
    this.cell = "";
    this.cellQuoted = false;
  }

  private endRow(): void {
    const blank = this.row.length === 0 && this.cell === "" && !this.cellQuoted;
    this.endCell();
    if (!blank) this.rows.push(this.row);
    this.row = [];
  }
}

/** Lines of the start of a file looked at to choose the delimiter. */
const SAMPLE_ROWS = 20;
const SAMPLE_CHARS = 64 * 1024;

/**
 * The delimiter that splits the first lines most evenly: the most lines with
 * as many cells as the first one (more than one), then the most cells; a
 * comma when none splits them. Quoted delimiters do not count.
 */
export function detectDelimiter(text: string): CsvDelimiter {
  const whole = text.length <= SAMPLE_CHARS;
  const sample = whole ? text : text.slice(0, SAMPLE_CHARS);
  let best: { delimiter: CsvDelimiter; even: number; cells: number } = { delimiter: ",", even: 0, cells: 1 };
  for (const delimiter of CSV_DELIMITERS) {
    const parser = new CsvParser(delimiter);
    parser.push(sample);
    let rows = whole ? parser.end() : parser.rows;
    rows = rows.slice(0, SAMPLE_ROWS);
    const cells = rows[0]?.length ?? 0;
    if (cells < 2) continue;
    const even = rows.filter((row) => row.length === cells).length;
    if (even > best.even || (even === best.even && cells > best.cells)) best = { delimiter, even, cells };
  }
  return best.delimiter;
}

function tableOf(parser: CsvParser, delimiter: CsvDelimiter): CsvTable {
  const [header = [], ...rows] = parser.end();
  return { delimiter, header, rows, openQuote: parser.openQuote };
}

/** A whole CSV text as a table: the first row is the header. */
export function parseCsv(text: string, delimiter: CsvDelimiter = detectDelimiter(text)): CsvTable {
  const parser = new CsvParser(delimiter);
  parser.push(text);
  return tableOf(parser, delimiter);
}

export interface ParseOptions {
  delimiter?: CsvDelimiter;
  /** Characters read between two pauses that let the page draw. */
  slice?: number;
  /** How much of the text is read, from 0 to 1. */
  onProgress?: (share: number) => void;
  signal?: AbortSignal;
}

const SLICE_CHARS = 2 * 1024 * 1024;

/** `parseCsv` a slice at a time, pausing between slices so a large file does not freeze the page. */
export async function parseCsvAsync(text: string, options: ParseOptions = {}): Promise<CsvTable> {
  const delimiter = options.delimiter ?? detectDelimiter(text);
  const slice = options.slice ?? SLICE_CHARS;
  const parser = new CsvParser(delimiter);
  for (let start = 0; start < text.length; start += slice) {
    options.signal?.throwIfAborted();
    parser.push(text.slice(start, start + slice));
    options.onProgress?.(Math.min(1, (start + slice) / text.length));
    if (start + slice < text.length) await pause();
  }
  return tableOf(parser, delimiter);
}

/** A CSV file read as UTF-8 and parsed (see `parseCsvAsync`). */
export async function readCsvFile(file: Blob, options: ParseOptions = {}): Promise<CsvTable> {
  return parseCsvAsync(await readFileText(file), options);
}

/** Lets the page draw before going on. */
export function pause(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

/** A file's text, read as UTF-8 (a byte-order mark dropped). */
export function readFileText(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error ?? new Error("could not read the file"));
    reader.readAsText(file, "utf-8");
  });
}

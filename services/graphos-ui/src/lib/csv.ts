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

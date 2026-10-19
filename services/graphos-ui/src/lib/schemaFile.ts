import type { SchemaDocument } from "../api/types";

/** The schema file format graphos exports, and the newest version this UI reads. */
export const SCHEMA_FORMAT = "graphos.schema";
export const SCHEMA_VERSION = 3;

/** A tenant name as graphos accepts it. */
export const TENANT_NAME = /^[A-Za-z][A-Za-z0-9_]{0,62}$/;

export type SchemaFileProblem = { kind: "notJson" } | { kind: "notSchema" } | { kind: "newerVersion"; version: number };

/**
 * Reads a schema file's text: the document, or why it is not one. A body
 * without an `_export` header (written by hand) is accepted, as graphos does.
 */
export function readSchemaFile(text: string): { doc: SchemaDocument } | { problem: SchemaFileProblem } {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return { problem: { kind: "notJson" } };
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return { problem: { kind: "notSchema" } };
  const doc = value as SchemaDocument;
  const header = doc._export;
  if (header !== undefined) {
    if (!header || typeof header !== "object" || (header.format ?? SCHEMA_FORMAT) !== SCHEMA_FORMAT) {
      return { problem: { kind: "notSchema" } };
    }
    if (typeof header.version === "number" && header.version > SCHEMA_VERSION) {
      return { problem: { kind: "newerVersion", version: header.version } };
    }
  }
  if (typeof doc.name !== "string") return { problem: { kind: "notSchema" } };
  return { doc };
}

/** A chosen file's text, read in the page (nothing is stored). */
export function readFileText(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error ?? new Error("could not read the file"));
    reader.readAsText(file);
  });
}

/** Whether a `create_tenants` pattern list (with `*` wildcards) covers `name`. */
export function mayCreate(patterns: string[], admin: boolean, name: string): boolean {
  if (admin) return true;
  return patterns.some((pattern) => new RegExp(`^${pattern.split("*").map(escapeRegExp).join(".*")}$`).test(name));
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

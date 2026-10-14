import type { CypherCell, CypherEdge, CypherNode, SavedQueryParam } from "../../api/types";

/** The query without its strings and comments, for looking at its words. */
function code(query: string): string {
  return query
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/\/\/[^\n]*/g, " ")
    .replace(/'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`[^`]*`/g, " ");
}

/** The `$name` parameters a query uses, in the order they first appear. */
export function queryParams(query: string): string[] {
  const names: string[] = [];
  for (const match of code(query).matchAll(/\$([A-Za-z_][A-Za-z0-9_]*)/g)) {
    if (!names.includes(match[1]!)) names.push(match[1]!);
  }
  return names;
}

/**
 * A parameter as typed in the form: its declared type when the query was
 * saved with one; otherwise a number, `true` / `false`, or a JSON list when
 * it reads as one, else the text itself.
 */
export function paramValue(text: string, type?: SavedQueryParam["type"]): unknown {
  const trimmed = text.trim();
  if (type === "string") return text;
  if (type === "number" || (!type && /^-?\d+(\.\d+)?$/.test(trimmed))) return Number(trimmed);
  if (type === "boolean" || (!type && (trimmed === "true" || trimmed === "false"))) return trimmed === "true";
  if (type === "list" || (!type && trimmed.startsWith("["))) {
    try {
      const value: unknown = JSON.parse(trimmed);
      if (Array.isArray(value)) return value;
    } catch {
      /* not a list: the text */
    }
    return type === "list" ? [] : text;
  }
  return text;
}

export function isNode(cell: CypherCell): cell is { node: CypherNode } {
  return typeof cell === "object" && cell !== null && !Array.isArray(cell) && "node" in cell;
}

export function isEdge(cell: CypherCell): cell is { edge: CypherEdge } {
  return typeof cell === "object" && cell !== null && !Array.isArray(cell) && "edge" in cell;
}

/** A cell as one line of text: `Label d1`, `TYPE`, a path as `a → b`, a value as JSON. */
export function cellText(cell: CypherCell): string {
  if (cell === null || cell === undefined) return "";
  if (isNode(cell)) return `${cell.node.label} ${cell.node.id}`;
  if (isEdge(cell)) return cell.edge.type;
  if (Array.isArray(cell) && cell.length > 0 && cell.every((c) => isNode(c) || isEdge(c))) {
    return cell.map(cellText).join(" → ");
  }
  return typeof cell === "object" ? JSON.stringify(cell) : String(cell);
}

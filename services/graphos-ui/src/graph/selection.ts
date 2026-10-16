import type { Properties } from "../api/types";
import { csvLine, csvStart } from "../lib/csv";
import type { GraphModel } from "./GraphModel";

export interface Point {
  x: number;
  y: number;
}

/** Whether `point` lies inside the polygon (even-odd rule, as a canvas path fills). */
export function insidePolygon(point: Point, polygon: Point[]): boolean {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const a = polygon[i]!;
    const b = polygon[j]!;
    if (a.y > point.y !== b.y > point.y && point.x < ((b.x - a.x) * (point.y - a.y)) / (b.y - a.y) + a.x) {
      inside = !inside;
    }
  }
  return inside;
}

/** `keys` toggled in `current`: added when absent, removed when present. */
export function toggled(current: ReadonlySet<string>, keys: Iterable<string>): Set<string> {
  const next = new Set(current);
  for (const key of keys) {
    if (next.has(key)) next.delete(key);
    else next.add(key);
  }
  return next;
}

/** The selected nodes still on the canvas, "+N more" placeholders left out. */
export function selectedNodes(model: GraphModel, keys: ReadonlySet<string>): string[] {
  return [...keys].filter((key) => model.graph.hasNode(key) && model.graph.getNodeAttribute(key, "more") === undefined);
}

/** The selected nodes as CSV: label, id, then every property any of them carries. */
export function selectionCsv(model: GraphModel, keys: ReadonlySet<string>): string {
  const rows = selectedNodes(model, keys).map((key) => model.graph.getNodeAttributes(key));
  const names = [...new Set(rows.flatMap((node) => Object.keys(node.properties)))]
    .filter((name) => name !== "id")
    .sort();
  const value = (properties: Properties, name: string) => properties[name] ?? "";
  return (
    csvStart(["label", "id", ...names]) +
    rows
      .map((node) => csvLine([node.vertexLabel, node.id, ...names.map((name) => value(node.properties, name))]))
      .join("")
  );
}

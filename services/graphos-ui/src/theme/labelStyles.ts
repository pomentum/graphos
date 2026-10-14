import type { DisplaySpec, EdgeDisplay, Properties, TenantSchema, VertexDisplay } from "../api/types";
import { CATEGORY_COLORS } from "./categories";

/**
 * The tenant's label styles (`display` in its schema), as the canvas, the
 * legend and the tables use them: a child label without its own colour or
 * icon takes its parent's, so a family reads as one.
 */
export interface LabelStyles {
  vertex: Map<string, VertexDisplay>;
  edge: Map<string, EdgeDisplay>;
}

export const NO_STYLES: LabelStyles = { vertex: new Map(), edge: new Map() };

/** Without nulls: the server answers keys an entry leaves out as null. */
function clean<T extends object>(entry: T | null | undefined): T | null {
  if (!entry) return null;
  const out = Object.fromEntries(Object.entries(entry).filter(([, v]) => v !== null && v !== undefined)) as T;
  return Object.keys(out).length ? out : null;
}

export function labelStyles(schema: Pick<TenantSchema, "label_hierarchy" | "display"> | undefined): LabelStyles {
  const display: DisplaySpec = schema?.display ?? {};
  const own = new Map<string, VertexDisplay>();
  for (const [label, entry] of Object.entries(display.vertex ?? {})) {
    const kept = clean(entry);
    if (kept) own.set(label, kept);
  }
  const hierarchy = schema?.label_hierarchy ?? {};
  const vertex = new Map(own);
  for (const child of Object.keys(hierarchy)) {
    // The nearest ancestor's colour and icon, for what the child does not declare.
    const inherited: VertexDisplay = {};
    const seen = new Set([child]);
    for (let parent = hierarchy[child]; parent && !seen.has(parent); parent = hierarchy[parent]) {
      seen.add(parent);
      const style = own.get(parent);
      inherited.color ??= style?.color;
      inherited.icon ??= style?.icon;
    }
    const mine = own.get(child) ?? {};
    const merged = clean({
      color: mine.color ?? inherited.color,
      icon: mine.icon ?? inherited.icon,
      ...stripped(mine),
    });
    if (merged) vertex.set(child, merged);
  }
  const edge = new Map<string, EdgeDisplay>();
  for (const [type, entry] of Object.entries(display.edge ?? {})) {
    const kept = clean(entry);
    if (kept) edge.set(type, kept);
  }
  return { vertex, edge };
}

function stripped(entry: VertexDisplay): VertexDisplay {
  const { color: _color, icon: _icon, ...rest } = entry;
  return rest;
}

/**
 * Colours for categories ranked most common first: a declared colour wins;
 * the others take the category colours in rank order, skipping those a
 * declared colour already took, so no two in the legend look alike.
 */
export function styledColors(
  ranked: readonly string[],
  declared: Map<string, { color?: string | null }>,
): Map<string, string> {
  const colors = new Map<string, string>();
  const taken = new Set<string>();
  for (const category of ranked) {
    const color = declared.get(category)?.color;
    if (color) {
      colors.set(category, color);
      taken.add(color.toUpperCase());
    }
  }
  const free = CATEGORY_COLORS.filter((c) => !taken.has(c.toUpperCase()));
  let next = 0;
  for (const category of ranked) {
    if (colors.has(category)) continue;
    if (next >= free.length) break;
    colors.set(category, free[next++]!);
  }
  return colors;
}

/** The name drawn for a node: its label's caption property when it has one, else `name`, else the id. */
export function captionOf(properties: Properties, id: string, style: VertexDisplay | EdgeDisplay | undefined): string {
  const caption = style?.caption ? properties[style.caption] : undefined;
  if ((typeof caption === "string" && caption) || typeof caption === "number") return String(caption);
  const name = properties.name;
  return typeof name === "string" && name ? name : id;
}

import type { Direction } from "../../api/types";

/** A path end: the node's id and label, and the name shown for it. */
export interface Endpoint {
  id: string;
  label: string;
  name: string;
}

/** What Path searches for, kept in the address so it survives moving between views. */
export interface PathState {
  from: Endpoint | null;
  to: Endpoint | null;
  direction: Direction;
  maxHops: number;
  relTypes: string[];
}

export const DEFAULT_HOPS = 4;
export const MAX_HOPS = 8;
const DIRECTIONS: Direction[] = ["both", "out", "in"];

/** Reads `?from=Label:id&fromName=…&to=…&toName=…&dir=…&hops=…&rel=…`; anything malformed takes its default. */
export function readPathState(params: URLSearchParams): PathState {
  const hops = Number(params.get("hops"));
  const dir = params.get("dir") as Direction | null;
  return {
    from: readEndpoint(params.get("from"), params.get("fromName")),
    to: readEndpoint(params.get("to"), params.get("toName")),
    direction: dir && DIRECTIONS.includes(dir) ? dir : "both",
    maxHops: Number.isInteger(hops) && hops >= 1 && hops <= MAX_HOPS ? hops : DEFAULT_HOPS,
    relTypes: params.getAll("rel").filter(Boolean),
  };
}

/** The query string for a state; the inverse of `readPathState`. Defaults are left out. */
export function pathParams(state: PathState): URLSearchParams {
  const params = new URLSearchParams();
  if (state.from) {
    params.set("from", `${state.from.label}:${state.from.id}`);
    params.set("fromName", state.from.name);
  }
  if (state.to) {
    params.set("to", `${state.to.label}:${state.to.id}`);
    params.set("toName", state.to.name);
  }
  if (state.direction !== "both") params.set("dir", state.direction);
  if (state.maxHops !== DEFAULT_HOPS) params.set("hops", String(state.maxHops));
  for (const rel of state.relTypes) params.append("rel", rel);
  return params;
}

/** `Label:id` (the id may itself contain colons) and its shown name. */
function readEndpoint(value: string | null, name: string | null): Endpoint | null {
  if (!value) return null;
  const colon = value.indexOf(":");
  if (colon <= 0 || colon === value.length - 1) return null;
  const id = value.slice(colon + 1);
  return { label: value.slice(0, colon), id, name: name || id };
}

/**
 * Undo for the Schema page. Every schema change is written to graphos at once,
 * so undoing one means writing its opposite: a style, property definition or
 * deprecation declared again as it was (or removed with `null`), a declared
 * label pair removed or declared again, a rename made back, a new and still
 * empty label or type deprecated and dropped.
 *
 * Some changes have no opposite graphos accepts: a merge never removes a
 * parent label or a search index, and a dropped label, an imported schema or a
 * new schema manager cannot be taken back. Such a change empties the stack,
 * and the page says why, rather than letting Ctrl+Z undo something older that
 * the person no longer has in mind.
 *
 * The stack is kept per tenant for the session (a reload forgets it). Undoing
 * sends the schema's version like any schema write: if someone else saved the
 * schema since, graphos refuses (412) and the stack is emptied.
 */
import { useSyncExternalStore } from "react";
import type { RenameKind, SchemaAddition, SchemaRenameRequest, TenantSchema } from "../api/types";

/** One write that undoes (part of) a change. */
export type UndoOp =
  | { kind: "add"; addition: SchemaAddition }
  | { kind: "removeEndpoint"; type: string; src: string; dst: string }
  | { kind: "rename"; request: SchemaRenameRequest }
  | { kind: "drop"; name: string };

/** What the person did, for the button's tooltip: an i18n key under `views.schema.undo.steps` and its values. */
export interface StepLabel {
  key: StepKey;
  params?: StepParams;
}

/** The names a step's label shows. */
export interface StepParams {
  [value: string]: string | undefined;
  name?: string;
  type?: string;
  src?: string;
  dst?: string;
  from?: string;
  to?: string;
  part?: string;
}

export type StepKey =
  | "style"
  | "properties"
  | "propertyChecks"
  | "deprecation"
  | "strictEndpoints"
  | "endpointAdded"
  | "endpointRemoved"
  | "rename"
  | "newLabel"
  | "newType"
  | "searchIndex"
  | "change"
  | "parent"
  | "dropped"
  | "imported"
  | "manager";

/** A change that can be undone: its label, and the writes that undo it, in order. */
export interface UndoStep {
  label: StepLabel;
  ops: UndoOp[];
}

/** A schema change as the page made it. */
export type SchemaChange =
  | { kind: "add"; addition: SchemaAddition }
  | { kind: "rename"; request: SchemaRenameRequest }
  | { kind: "removeEndpoint"; type: string; src: string; dst: string }
  | { kind: "drop"; name: string }
  | { kind: "import" }
  | { kind: "manager" };

/** What undoes a change, or what to say when nothing can: `{ irreversible }`. */
export type Inverse = UndoStep | { irreversible: StepLabel };

const RENAME_PART: Record<RenameKind, string> = { label: "label", edge_type: "type", property: "property" };

function mapOf<T>(record: Record<string, T> | undefined): Record<string, T> {
  return record ?? {};
}

/** The previous value of each entry the addition declares (`null` where there was none). */
function previous<T>(declared: Record<string, unknown> | undefined, before: Record<string, T> | undefined) {
  const out: Record<string, T | null> = {};
  for (const name of Object.keys(declared ?? {})) out[name] = mapOf(before)[name] ?? null;
  return out;
}

/** A declared label pair: its edge type, source and target labels. */
type Pair = [type: string, src: string, dst: string];

const isEmpty = (record: Record<string, unknown> | undefined) => !record || Object.keys(record).length === 0;

/** The label of an addition: the part it changes and the name it changes, or "change" when several. */
function additionLabel(addition: SchemaAddition, newLabels: string[], newTypes: string[], newPairs: Pair[]): StepLabel {
  const parts: StepLabel[] = [];
  for (const name of newLabels) parts.push({ key: "newLabel", params: { name } });
  for (const name of newTypes) parts.push({ key: "newType", params: { name } });
  if (!newTypes.length)
    for (const [type, src, dst] of newPairs) parts.push({ key: "endpointAdded", params: { type, src, dst } });
  const named = (key: StepKey, spec?: { vertex?: Record<string, unknown>; edge?: Record<string, unknown> }) => {
    for (const name of [...Object.keys(spec?.vertex ?? {}), ...Object.keys(spec?.edge ?? {})])
      if (!newLabels.includes(name) && !newTypes.includes(name)) parts.push({ key, params: { name } });
  };
  named("style", addition.display);
  named("properties", addition.property_schema);
  named("deprecation", addition.deprecated);
  if (addition.property_schema?.mode) parts.push({ key: "propertyChecks" });
  if (addition.strict_endpoints !== undefined) parts.push({ key: "strictEndpoints" });
  for (const name of Object.keys(addition.search_indexes ?? {})) parts.push({ key: "searchIndex", params: { name } });
  return parts.length === 1 ? parts[0]! : { key: "change" };
}

/**
 * What undoes `change`, made from the schema as it was just before it: the
 * writes that put back what the change replaced, or `{ irreversible }` when
 * graphos has no way back. Only an addition needs `before`.
 */
export function inverseOf(before: TenantSchema | null, change: SchemaChange): Inverse {
  switch (change.kind) {
    case "rename": {
      const { kind, from, to, on } = change.request;
      return {
        label: { key: "rename", params: { from, to, part: RENAME_PART[kind] } },
        ops: [{ kind: "rename", request: { kind, from: to, to: from, ...(on ? { on } : {}) } }],
      };
    }
    case "removeEndpoint": {
      const { type, src, dst } = change;
      return {
        label: { key: "endpointRemoved", params: { type, src, dst } },
        ops: [{ kind: "add", addition: { edge_endpoints: { [type]: [[src, dst]] } } }],
      };
    }
    case "drop":
      return { irreversible: { key: "dropped", params: { name: change.name } } };
    case "import":
      return { irreversible: { key: "imported" } };
    case "manager":
      return { irreversible: { key: "manager" } };
    case "add":
      // Without the schema as it was, what the change replaced is not known.
      return before ? inverseOfAddition(before, change.addition) : { irreversible: { key: "change" } };
  }
}

function inverseOfAddition(before: TenantSchema, addition: SchemaAddition): Inverse {
  // A merge never removes a parent link; a search index can be put back as it was, but not removed.
  for (const [child, parent] of Object.entries(addition.label_hierarchy ?? {}))
    if (before.label_hierarchy[child] !== parent) return { irreversible: { key: "parent", params: { name: child } } };
  const newIndexes = Object.keys(addition.search_indexes ?? {}).filter((name) => !(name in before.search_indexes));
  if (newIndexes.length) return { irreversible: { key: "searchIndex", params: { name: newIndexes[0]! } } };

  const newLabels = (addition.vertex_labels ?? []).filter((name) => !before.vertex_labels.includes(name));
  const newTypes = (addition.edge_labels ?? []).filter((name) => !before.edge_labels.includes(name));
  const declared = mapOf(before.edge_endpoints);
  const newPairs: Pair[] = [];
  for (const [type, pairs] of Object.entries(addition.edge_endpoints ?? {}))
    for (const [src, dst] of pairs)
      if (!mapOf(declared)[type]?.some((p) => p.src === src && p.dst === dst)) newPairs.push([type, src, dst]);

  const back: SchemaAddition = {};
  if (addition.display) {
    back.display = {};
    if (!isEmpty(addition.display.vertex))
      back.display.vertex = previous(addition.display.vertex, before.display?.vertex);
    if (!isEmpty(addition.display.edge)) back.display.edge = previous(addition.display.edge, before.display?.edge);
    // An entry sent replaces the stored one, whoever sends it (a schema manager's default is missing_only).
    back.display_merge = "entries";
  }
  if (addition.property_schema) {
    const ps = addition.property_schema;
    back.property_schema = {};
    if (ps.mode) back.property_schema.mode = before.property_schema?.mode ?? "off";
    if (!isEmpty(ps.vertex)) back.property_schema.vertex = previous(ps.vertex, before.property_schema?.vertex);
    if (!isEmpty(ps.edge)) back.property_schema.edge = previous(ps.edge, before.property_schema?.edge);
  }
  if (addition.deprecated) {
    back.deprecated = {};
    if (!isEmpty(addition.deprecated.vertex))
      back.deprecated.vertex = previous(addition.deprecated.vertex, before.deprecated?.vertex);
    if (!isEmpty(addition.deprecated.edge))
      back.deprecated.edge = previous(addition.deprecated.edge, before.deprecated?.edge);
  }
  if (addition.strict_endpoints !== undefined) back.strict_endpoints = before.strict_endpoints ?? false;
  if (!isEmpty(addition.search_indexes))
    back.search_indexes = previous(addition.search_indexes, before.search_indexes) as SchemaAddition["search_indexes"];

  // A new label or type is taken back by deprecating it and dropping it, which graphos allows while it is empty.
  for (const name of newLabels)
    (back.deprecated ??= {}).vertex = { ...back.deprecated?.vertex, [name]: { deprecated: true } };
  for (const name of newTypes)
    (back.deprecated ??= {}).edge = { ...back.deprecated?.edge, [name]: { deprecated: true } };

  const ops: UndoOp[] = [];
  // Pairs of a type that is dropped go with it.
  for (const [type, src, dst] of newPairs)
    if (!newTypes.includes(type)) ops.push({ kind: "removeEndpoint", type, src, dst });
  if (Object.keys(back).length) ops.push({ kind: "add", addition: back });
  for (const name of [...newTypes, ...newLabels]) ops.push({ kind: "drop", name });
  return { label: additionLabel(addition, newLabels, newTypes, newPairs), ops };
}

// ── The stack, per tenant, for the session ──────────────────────────────────

/** Steps kept per tenant; the oldest go first. */
export const UNDO_LIMIT = 50;

export interface UndoState {
  steps: UndoStep[];
  /** The last change that emptied the stack because it cannot be undone, until the next undoable one. */
  blocked: StepLabel | null;
}

const EMPTY: UndoState = { steps: [], blocked: null };
const states = new Map<string, UndoState>();
const listeners = new Set<() => void>();

function set(tenant: string, state: UndoState) {
  states.set(tenant, state);
  for (const listener of listeners) listener();
}

export const schemaUndo = {
  get: (tenant: string): UndoState => states.get(tenant) ?? EMPTY,
  /** Records what undoes a change just made. */
  record(tenant: string, inverse: Inverse) {
    if ("irreversible" in inverse) set(tenant, { steps: [], blocked: inverse.irreversible });
    else set(tenant, { steps: [...schemaUndo.get(tenant).steps, inverse].slice(-UNDO_LIMIT), blocked: null });
  },
  /** Takes the last step off the stack. */
  pop(tenant: string): UndoStep | null {
    const { steps } = schemaUndo.get(tenant);
    const last = steps.at(-1) ?? null;
    if (last) set(tenant, { steps: steps.slice(0, -1), blocked: null });
    return last;
  },
  clear(tenant: string) {
    if (states.has(tenant)) set(tenant, EMPTY);
  },
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => void listeners.delete(listener);
  },
};

/** The tenant's undo stack, kept current. */
export function useSchemaUndoState(tenant: string): UndoState {
  return useSyncExternalStore(schemaUndo.subscribe, () => schemaUndo.get(tenant));
}

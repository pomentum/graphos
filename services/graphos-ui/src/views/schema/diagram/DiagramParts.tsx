import {
  BaseEdge,
  EdgeLabelRenderer,
  Handle,
  Position,
  useConnection,
  useInternalNode,
  type Edge,
  type EdgeProps,
  type InternalNode,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { createContext, useContext } from "react";
import { useTranslation } from "react-i18next";
import { freeGeometry, type Box, type Link, type Point, type Rect, type Selection } from "./diagramModel";
import classes from "./SchemaDiagram.module.css";

/** What every box and line on the diagram needs to know to draw itself. */
export interface DiagramState {
  focus: { labels: Set<string>; links: Set<string> } | null;
  selection: Selection;
  /** Drawing a relationship: dragging from a box draws a line. */
  drawing: boolean;
  colours: Map<string, string>;
  select: (selection: Selection) => void;
}

export const DiagramContext = createContext<DiagramState>({
  focus: null,
  selection: null,
  drawing: false,
  colours: new Map(),
  select: () => undefined,
});

export type LabelNode = Node<{ box: Box; meta: string }, "label">;
export type FamilyNode = Node<{ root: string; size: number; nested: boolean }, "family">;
export type DiagramNode = LabelNode | FamilyNode;

export interface LineData extends Record<string, unknown> {
  link: Link;
  /** The reverse line also exists: a free line bows aside. */
  bowed: boolean;
  /** A line of a type that declares no pairs, drawn for All: faint, captioned only while lit. */
  extra: boolean;
  /** Where its caption goes, placed with the others' (null: the middle of the line). */
  captionAt: Point | null;
}

export type LineEdge = Edge<LineData, "line">;

/** One label: its name, node count and properties, coloured by its family. */
export function LabelBox({ id, data }: NodeProps<LabelNode>) {
  const state = useContext(DiagramContext);
  const connection = useConnection();
  const { box, meta } = data;
  const faded = state.focus !== null && !state.focus.labels.has(id);
  const selected = state.selection?.kind === "label" && state.selection.id === id;
  const target = connection.inProgress && connection.toNode?.id === id;
  const colour = state.colours.get(box.family);
  return (
    <div
      className={classes.box}
      data-empty={box.empty || undefined}
      data-faded={faded || undefined}
      data-selected={selected || undefined}
      data-target={target || undefined}
      style={colour ? { borderLeftColor: colour } : undefined}
      title={box.label}
    >
      <div className={classes.boxName}>{box.label}</div>
      {meta ? <div className={classes.boxMeta}>{meta}</div> : null}
      {/* While drawing, the whole box starts a line and the whole box takes one. */}
      <Handle
        type="source"
        position={Position.Bottom}
        className={state.drawing && !connection.inProgress ? classes.handleWhole : classes.handleHidden}
        isConnectable={state.drawing}
      />
      <Handle
        type="target"
        position={Position.Top}
        className={state.drawing && connection.inProgress ? classes.handleWhole : classes.handleHidden}
        isConnectable={state.drawing}
        isConnectableStart={false}
      />
    </div>
  );
}

function rectOf(node: InternalNode): Rect {
  const w = node.measured.width ?? 0;
  const h = node.measured.height ?? 0;
  return { x: node.internals.positionAbsolute.x + w / 2, y: node.internals.positionAbsolute.y + h / 2, w, h };
}

/**
 * One relationship line: a curve from box to box ending in an arrow,
 * carrying its types and edge counts; drawn by its status (`data-status`):
 * declared solid, outside the declarations dashed in the warning colour,
 * open (its types declare no pairs) dotted.
 */
export function SchemaLine({ id, source, target, data }: EdgeProps<LineEdge>) {
  const state = useContext(DiagramContext);
  const src = useInternalNode(source);
  const dst = useInternalNode(target);
  if (!src || !dst || !data) return null;
  const { link, bowed, extra, captionAt } = data;

  const { d: path, caption: middle } = freeGeometry(rectOf(src), rectOf(dst), source === target, bowed);
  const at = captionAt ?? middle;

  const selected = state.selection?.kind === "link" && state.selection.id === id;
  const lit = selected || (state.focus?.links.has(id) ?? false);
  const faded = state.focus !== null && !lit;
  const inherits = link.kind === "inherits";
  const marker = inherits
    ? lit
      ? "gx-inherits-lit"
      : "gx-inherits"
    : link.status === "undeclared"
      ? "gx-arrow-warn"
      : lit
        ? "gx-arrow-lit"
        : "gx-arrow";

  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        markerEnd={`url(#${marker})`}
        interactionWidth={16}
        className={classes.line}
        data-kind={link.kind}
        data-lit={lit || undefined}
        data-faded={faded || undefined}
        data-status={link.status}
        data-extra={extra || undefined}
      />
      {link.caption && (!extra || lit) ? (
        <EdgeLabelRenderer>
          <button
            type="button"
            className={`${classes.caption} nodrag nopan`}
            data-lit={lit || undefined}
            data-faded={faded || undefined}
            data-status={link.status}
            style={{ transform: `translate(-50%, -50%) translate(${at.x}px, ${at.y}px)` }}
            onClick={() => state.select({ kind: "link", id })}
          >
            {link.caption}
          </button>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}

/** A block: a label and the labels inheriting from it, its name and size on top. */
export function FamilyBlock({ data }: NodeProps<FamilyNode>) {
  const { t } = useTranslation();
  const state = useContext(DiagramContext);
  const colour = state.colours.get(data.root);
  return (
    <div
      className={classes.family}
      style={colour ? { borderColor: colour, ["--family" as string]: colour } : undefined}
    >
      <div className={classes.familyTitle}>
        <span className={classes.familyName}>{data.root}</span>
        <span className={classes.familySize}>{t("views.schema.diagram.familySize", { count: data.size })}</span>
      </div>
    </div>
  );
}

/** The arrowheads the lines use: idle, lit, and for pairs outside the declarations. */
export function LineMarkers() {
  const head = (id: string, open: boolean, lit: boolean | "warn") => (
    <marker
      id={id}
      viewBox="0 0 12 12"
      refX="11"
      refY="6"
      markerWidth="12"
      markerHeight="12"
      markerUnits="userSpaceOnUse"
      orient="auto-start-reverse"
    >
      <path
        d={open ? "M 1 1 L 11 6 L 1 11 Z" : "M 1 2 L 11 6 L 1 10 Z"}
        className={lit === "warn" ? classes.markerWarn : lit ? classes.markerLit : classes.marker}
        data-open={open || undefined}
      />
    </marker>
  );
  return (
    <svg className={classes.markers} aria-hidden="true">
      <defs>
        {head("gx-arrow", false, false)}
        {head("gx-arrow-lit", false, true)}
        {head("gx-arrow-warn", false, "warn")}
        {head("gx-inherits", true, false)}
        {head("gx-inherits-lit", true, true)}
      </defs>
    </svg>
  );
}

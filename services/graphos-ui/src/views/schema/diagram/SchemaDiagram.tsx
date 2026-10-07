import {
  ActionIcon,
  Alert,
  Button,
  Group,
  Loader,
  Switch,
  Text,
  Tooltip,
  UnstyledButton,
  useComputedColorScheme,
} from "@mantine/core";
import {
  IconChevronDown,
  IconChevronRight,
  IconInfoCircle,
  IconLayoutDistributeVertical,
  IconLine,
  IconPlus,
  IconX,
} from "@tabler/icons-react";
import {
  applyEdgeChanges,
  applyNodeChanges,
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection,
  type EdgeChange,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { GraphMap, TenantSchema } from "../../../api/types";
import { formatCount } from "../../../lib/time";
import { CATEGORY_COLORS } from "../../../theme/categories";
import { usePalette } from "../../../theme/usePalette";
import { DiagramPanel } from "./DiagramPanel";
import {
  DiagramContext,
  FamilyBlock,
  LabelBox,
  LineMarkers,
  SchemaLine,
  type DiagramNode,
  type DiagramState,
  type FamilyNode,
  type LabelNode,
  type LineEdge,
} from "./DiagramParts";
import {
  buildDiagram,
  familyColours,
  familyId,
  focusOf,
  layoutKey,
  loadPositions,
  savePositions,
  type Block,
  type Box,
  type Diagram,
  type DiagramText,
  type Layout,
  type Point,
  type Positions,
  type Rect,
  type Selection,
} from "./diagramModel";
import { layoutDiagram } from "./elkLayout";
import classes from "./SchemaDiagram.module.css";

/** A relationship type drawn between two labels here, before any edge uses it. */
export interface DraftLine {
  type: string;
  src: string;
  dst: string;
}

export interface SchemaDiagramProps {
  tenant: string;
  schema: TenantSchema;
  map: GraphMap | undefined;
  /** Property names by label, from the catalogue. */
  properties: Map<string, string[]>;
  /** Administrators add labels and draw relationship types. */
  editable: boolean;
  drafts: DraftLine[];
  onAddLabel: () => void;
  onDrawRelationship: (src: string, dst: string) => void;
}

const NODE_TYPES = { label: LabelBox, family: FamilyBlock };
const EDGE_TYPES = { line: SchemaLine };
const FIT = { padding: 0.12, maxZoom: 1.2 };
const LEGEND_KEY = "graphos.schemaDiagram.legend";

/** Whether the legend was left open, in this browser (open when unknown). */
function legendWasOpen(): boolean {
  try {
    return window.localStorage.getItem(LEGEND_KEY) !== "hidden";
  } catch {
    return true;
  }
}

function rememberLegend(open: boolean): void {
  try {
    window.localStorage.setItem(LEGEND_KEY, open ? "shown" : "hidden");
  } catch {
    /* not kept */
  }
}

/**
 * The schema drawn as a tidy grid: a box per label, each inheritance family
 * in a block (root on top, children under their parents, dashed lines to
 * the parent), the other labels after them, busiest first; ELK packs it.
 * Relationship lines are drawn for the selected label (all of them, with
 * their types and edge counts), or for every label with All lines. Boxes and
 * blocks can be moved (remembered in this browser) and put back with Tidy
 * up. Administrators add labels here and draw a relationship type by
 * dragging from one box to another.
 */
/**
 * The canvas nodes for a layout: each block before what it holds (as React
 * Flow needs), everything placed relative to the block around it, the lone
 * labels on their own. Positions the user saved win over the layout's.
 */
function placeNodes(diagram: Diagram, laid: Layout, kept: Positions, meta: (box: Box) => string): DiagramNode[] {
  const boxes = new Map(diagram.boxes.map((b) => [b.label, b] as const));
  const nodes: DiagramNode[] = [];
  const inside = (at: Point, parent: Rect | undefined) => (parent ? { x: at.x - parent.x, y: at.y - parent.y } : at);
  const place = (item: string | Block, parentId: string | undefined) => {
    const parent = parentId ? laid.blocks[parentId] : undefined;
    const nesting = parentId ? { parentId, extent: "parent" as const } : {};
    if (typeof item === "string") {
      const box = boxes.get(item);
      const at = laid.positions[item];
      if (!box || !at) return;
      nodes.push({
        id: item,
        type: "label",
        position: kept[item] ?? inside(at, parent),
        width: box.w,
        height: box.h,
        ...nesting,
        data: { box, meta: meta(box) },
      });
      return;
    }
    const id = familyId(item.root);
    const rect = laid.blocks[id];
    if (!rect) return;
    nodes.push({
      id,
      type: "family",
      position: kept[id] ?? inside(rect, parent),
      width: rect.w,
      height: rect.h,
      selectable: false,
      ...nesting,
      data: { root: item.root, size: item.size, nested: Boolean(parentId) },
    });
    item.items.forEach((inner) => place(inner, id));
  };
  [...diagram.blocks, ...diagram.lone].forEach((item) => place(item, undefined));
  return nodes;
}

export function SchemaDiagram(props: SchemaDiagramProps) {
  return (
    <ReactFlowProvider>
      <DiagramCanvas {...props} />
    </ReactFlowProvider>
  );
}

function DiagramCanvas({
  tenant,
  schema,
  map,
  properties,
  editable,
  drafts,
  onAddLabel,
  onDrawRelationship,
}: SchemaDiagramProps) {
  const { t } = useTranslation();
  const palette = usePalette();
  const scheme = useComputedColorScheme("light");
  const flow = useReactFlow();
  const [hideEmpty, setHideEmpty] = useState(false);
  const [allLines, setAllLines] = useState(false);
  const [legendOpen, setLegendOpen] = useState(legendWasOpen);
  const [unusedOpen, setUnusedOpen] = useState(false);
  const [drawing, setDrawing] = useState(false);
  const [selection, setSelection] = useState<Selection>(null);
  const [saved, setSaved] = useState<Positions>(() => loadPositions(tenant));
  const [layout, setLayout] = useState<{ key: string; result: Layout } | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [nodes, setNodes] = useState<DiagramNode[]>([]);
  const [edges, setEdges] = useState<LineEdge[]>([]);

  const text = useMemo<DiagramText>(
    () => ({
      nodes: (count) => t("common.counts.nodes", { count, formatted: formatCount(count) }),
      formatCount,
      more: (count) => t("views.schema.diagram.more", { count }),
    }),
    [t],
  );
  // A box's second line; the property counts arrive later and do not move anything.
  const meta = useCallback(
    (box: Box) =>
      [
        box.count === null ? null : text.nodes(box.count),
        properties.size ? t("views.schema.labels.properties", { count: properties.get(box.label)?.length ?? 0 }) : null,
      ]
        .filter(Boolean)
        .join(" · "),
    [t, text, properties],
  );
  const diagram = useMemo(() => buildDiagram(schema, map, { hideEmpty, text }), [schema, map, hideEmpty, text]);
  const colours = useMemo(() => familyColours(diagram.blocks, CATEGORY_COLORS), [diagram]);
  const key = useMemo(() => layoutKey(diagram), [diagram]);
  const relationshipLines = diagram.links.filter((l) => l.kind === "relationship").length;

  // Lay out again only when the boxes or the blocks change.
  useEffect(() => {
    let current = true;
    setFailed(null);
    layoutDiagram(diagram).then(
      (result) => current && setLayout({ key, result }),
      (error: unknown) => current && setFailed(error instanceof Error ? error.message : String(error)),
    );
    return () => {
      current = false;
    };
    // `key` stands for the diagram's layout: a new diagram object with the same key needs none.
  }, [key]);

  const laid = layout?.key === key ? layout.result : null;

  // Boxes where the user left them, else where ELK put them.
  useEffect(() => {
    if (!laid) return;
    setNodes(placeNodes(diagram, laid, loadPositions(tenant), meta));
    // Only a new layout places the boxes; moving one must not snap the rest back.
  }, [laid]);

  // New counts or properties: the boxes' text changes, not their places.
  useEffect(() => {
    const boxes = new Map(diagram.boxes.map((b) => [b.label, b] as const));
    setNodes((current) =>
      current.map((node) => {
        if (node.type !== "label") return node;
        const box = boxes.get(node.id) ?? node.data.box;
        return { ...node, data: { box, meta: meta(box) } };
      }),
    );
  }, [diagram, meta]);

  useEffect(() => {
    if (!laid) return;
    // Relationship lines with All lines, or those of the selection; blocks show inheritance.
    const touches = (l: { key: string; src: string; dst: string }) =>
      selection?.kind === "label" ? l.src === selection.id || l.dst === selection.id : selection?.id === l.key;
    const drawn = diagram.links.filter((l) => l.kind === "relationship" && (allLines || touches(l)));
    const pairs = new Set(drawn.map((l) => `${l.kind}|${l.src}|${l.dst}`));
    const shown = new Set(diagram.boxes.map((b) => b.label));
    const lines: LineEdge[] = drawn.map((link) => ({
      id: link.key,
      type: "line",
      source: link.src,
      target: link.dst,
      data: {
        link,
        bowed:
          link.kind === "relationship" && link.src !== link.dst && pairs.has(`relationship|${link.dst}|${link.src}`),
        draft: false,
        extra: !touches(link),
      },
    }));
    const existing = new Set(diagram.links.flatMap((l) => l.types.map((x) => `${x.type}|${l.src}|${l.dst}`)));
    for (const draft of drafts) {
      if (!shown.has(draft.src) || !shown.has(draft.dst) || existing.has(`${draft.type}|${draft.src}|${draft.dst}`)) {
        continue;
      }
      const key = `draft|${draft.type}|${draft.src}|${draft.dst}`;
      lines.push({
        id: key,
        type: "line",
        source: draft.src,
        target: draft.dst,
        data: {
          link: {
            key,
            src: draft.src,
            dst: draft.dst,
            kind: "relationship",
            types: [],
            count: 0,
            caption: `${draft.type} · ${t("views.schema.diagram.noEdgesYet")}`,
          },
          bowed:
            pairs.has(`relationship|${draft.dst}|${draft.src}`) || pairs.has(`relationship|${draft.src}|${draft.dst}`),
          draft: true,
          extra: false,
        },
      });
    }
    setEdges(lines);
  }, [laid, diagram, drafts, t, allLines, selection]);

  // Fit the view when the set of boxes changes (first layout, Hide empty labels).
  const boxKeys = useMemo(() => nodes.map((n) => n.id).join("|"), [nodes]);
  useEffect(() => {
    if (!boxKeys) return;
    const timer = window.setTimeout(() => void flow.fitView({ ...FIT, duration: 250 }), 30);
    return () => window.clearTimeout(timer);
  }, [boxKeys, flow]);

  const onNodesChange = useCallback((changes: NodeChange<DiagramNode>[]) => {
    setNodes((current) => applyNodeChanges(changes, current));
    const picked = changes.find((c) => c.type === "select" && c.selected);
    if (picked && "id" in picked && !picked.id.startsWith("family:")) setSelection({ kind: "label", id: picked.id });
  }, []);
  const onEdgesChange = useCallback((changes: EdgeChange<LineEdge>[]) => {
    setEdges((current) => applyEdgeChanges(changes, current));
    const picked = changes.find((c) => c.type === "select" && c.selected);
    if (picked && "id" in picked && !picked.id.startsWith("draft|")) setSelection({ kind: "link", id: picked.id });
  }, []);

  const onMoved = useCallback(
    (_event: unknown, _node: DiagramNode, moved: DiagramNode[]) => {
      setSaved((current) => {
        const next = { ...current };
        for (const node of moved) next[node.id] = { x: Math.round(node.position.x), y: Math.round(node.position.y) };
        savePositions(tenant, next);
        return next;
      });
    },
    [tenant],
  );

  const tidy = () => {
    if (!laid) return;
    setSaved({});
    savePositions(tenant, {});
    setNodes(placeNodes(diagram, laid, {}, meta));
    window.setTimeout(() => void flow.fitView({ ...FIT, duration: 250 }), 30);
  };

  const onConnect = useCallback(
    (connection: Connection) => {
      setDrawing(false);
      onDrawRelationship(connection.source, connection.target);
    },
    [onDrawRelationship],
  );

  const state = useMemo<DiagramState>(
    () => ({ focus: focusOf(diagram, selection), selection, drawing, colours, select: setSelection }),
    [diagram, selection, drawing, colours],
  );
  const selectionShown =
    selection &&
    (selection.kind === "label"
      ? diagram.boxes.some((b) => b.label === selection.id)
      : diagram.links.some((l) => l.key === selection.id))
      ? selection
      : null;
  const moved = Object.keys(saved).length > 0;

  const aria = useMemo(
    () => ({
      "node.a11yDescription.default": t("views.schema.diagram.a11y.node"),
      "node.a11yDescription.keyboardDisabled": t("views.schema.diagram.a11y.node"),
      "edge.a11yDescription.default": t("views.schema.diagram.a11y.line"),
      "controls.ariaLabel": t("views.schema.diagram.a11y.controls"),
      "controls.zoomIn.ariaLabel": t("views.schema.diagram.a11y.zoomIn"),
      "controls.zoomOut.ariaLabel": t("views.schema.diagram.a11y.zoomOut"),
      "controls.fitView.ariaLabel": t("views.schema.diagram.a11y.fit"),
      "minimap.ariaLabel": t("views.schema.diagram.a11y.minimap"),
      "handle.ariaLabel": t("views.schema.diagram.a11y.handle"),
    }),
    [t],
  );

  return (
    <div className={classes.root} data-panel={selectionShown ? "open" : undefined}>
      <div className={classes.canvas} data-drawing={drawing || undefined}>
        <DiagramContext.Provider value={state}>
          <LineMarkers />
          <ReactFlow<DiagramNode, LineEdge>
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            edgeTypes={EDGE_TYPES}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onNodeDragStop={onMoved}
            onConnect={onConnect}
            onNodeClick={(_event, node) => node.type === "label" && setSelection({ kind: "label", id: node.id })}
            onEdgeClick={(_event, edge) => !edge.id.startsWith("draft|") && setSelection({ kind: "link", id: edge.id })}
            onPaneClick={() => setSelection(null)}
            nodesDraggable={!drawing}
            nodesConnectable={drawing}
            connectOnClick={false}
            deleteKeyCode={null}
            multiSelectionKeyCode={null}
            selectionKeyCode={null}
            colorMode={scheme}
            fitViewOptions={FIT}
            minZoom={0.1}
            maxZoom={2}
            ariaLabelConfig={aria}
            aria-label={t("views.schema.diagram.canvasLabel")}
            attributionPosition="bottom-center"
          >
            <Background variant={BackgroundVariant.Dots} gap={22} size={1.2} color={palette.edgeIdle} />
            <Controls position="bottom-left" showInteractive={false} fitViewOptions={FIT} />
            <MiniMap
              position="bottom-right"
              pannable
              zoomable
              nodeColor={(node) =>
                node.type === "family"
                  ? "transparent"
                  : (colours.get((node.data as LabelNode["data"]).box.family) ?? palette.nodeIdle)
              }
              nodeStrokeColor={(node) =>
                node.type === "family"
                  ? (colours.get((node.data as FamilyNode["data"]).root) ?? palette.edgeIdle)
                  : "none"
              }
              maskColor={scheme === "dark" ? "rgba(0,0,0,0.45)" : "rgba(255,255,255,0.55)"}
              bgColor={palette.panel}
            />
            <Panel position="top-left" className={classes.toolbar}>
              <Group gap="xs" wrap="wrap">
                <Switch
                  size="xs"
                  label={t("views.schema.diagram.hideEmpty")}
                  checked={hideEmpty}
                  onChange={(e) => setHideEmpty(e.currentTarget.checked)}
                />
                <Tooltip label={t("views.schema.diagram.allLinesHint")} withArrow multiline w={260}>
                  <Switch
                    size="xs"
                    label={t("views.schema.diagram.allLinesCount", { count: relationshipLines })}
                    disabled={relationshipLines === 0}
                    checked={allLines}
                    onChange={(e) => setAllLines(e.currentTarget.checked)}
                  />
                </Tooltip>
                <Tooltip label={t("views.schema.diagram.tidyHint")} withArrow>
                  <Button
                    size="compact-xs"
                    variant="default"
                    leftSection={<IconLayoutDistributeVertical size={14} />}
                    disabled={!laid || !moved}
                    onClick={tidy}
                  >
                    {t("views.schema.diagram.tidy")}
                  </Button>
                </Tooltip>
                {editable ? (
                  <>
                    <Button
                      size="compact-xs"
                      variant="default"
                      leftSection={<IconPlus size={14} />}
                      onClick={onAddLabel}
                    >
                      {t("views.schema.addLabel")}
                    </Button>
                    <Button
                      size="compact-xs"
                      variant={drawing ? "filled" : "default"}
                      leftSection={<IconLine size={14} />}
                      aria-pressed={drawing}
                      onClick={() => setDrawing((on) => !on)}
                    >
                      {t("views.schema.diagram.draw")}
                    </Button>
                  </>
                ) : null}
              </Group>
              {drawing ? (
                <Text size="xs" c="dimmed" mt={6}>
                  {t("views.schema.diagram.drawHint")}
                </Text>
              ) : null}
            </Panel>
            {legendOpen ? (
              <Panel position="top-right" className={classes.legend}>
                <ActionIcon
                  className={classes.legendClose}
                  size="sm"
                  variant="subtle"
                  color="gray"
                  aria-label={t("views.schema.diagram.hideLegend")}
                  onClick={() => {
                    setLegendOpen(false);
                    rememberLegend(false);
                  }}
                >
                  <IconX size={14} />
                </ActionIcon>
                <div className={classes.legendRow}>
                  <svg width="34" height="10" aria-hidden="true">
                    <path d="M 1 5 L 31 5" className={classes.legendLine} markerEnd="url(#gx-arrow)" />
                  </svg>
                  <Text size="xs" c="dimmed">
                    {t("views.schema.diagram.legendRelationship")}
                  </Text>
                </div>
                <div className={classes.legendRow}>
                  <span className={classes.legendBlock} aria-hidden="true" />
                  <Text size="xs" c="dimmed">
                    {t("views.schema.diagram.legendBlock")}
                  </Text>
                </div>
                {diagram.unusedTypes.length ? (
                  <>
                    <UnstyledButton
                      className={classes.unusedToggle}
                      aria-expanded={unusedOpen}
                      onClick={() => setUnusedOpen((open) => !open)}
                    >
                      {unusedOpen ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
                      <Text size="xs" c="dimmed">
                        {t("views.schema.diagram.unusedCount", { count: diagram.unusedTypes.length })}
                      </Text>
                    </UnstyledButton>
                    {unusedOpen ? (
                      <Text size="xs" c="dimmed" className={classes.unused}>
                        {diagram.unusedTypes.join(", ")}
                      </Text>
                    ) : null}
                  </>
                ) : null}
              </Panel>
            ) : (
              <Panel position="top-right">
                <Tooltip label={t("views.schema.diagram.showLegend")} withArrow position="left">
                  <ActionIcon
                    variant="default"
                    aria-label={t("views.schema.diagram.showLegend")}
                    onClick={() => {
                      setLegendOpen(true);
                      rememberLegend(true);
                    }}
                  >
                    <IconInfoCircle size={16} />
                  </ActionIcon>
                </Tooltip>
              </Panel>
            )}
          </ReactFlow>
          {!laid && !failed ? (
            <div className={classes.status}>
              <Loader size="sm" />
              <Text size="xs" c="dimmed">
                {t("views.schema.diagram.laying")}
              </Text>
            </div>
          ) : null}
          {failed ? (
            <div className={classes.status}>
              <Alert color="red" variant="light" radius="md" title={t("views.schema.diagram.layoutFailed")}>
                {failed}
              </Alert>
            </div>
          ) : null}
          {laid && diagram.boxes.length === 0 ? (
            <div className={classes.status}>
              <Text size="sm" c="dimmed">
                {t("views.schema.diagram.empty")}
              </Text>
            </div>
          ) : null}
        </DiagramContext.Provider>
      </div>
      {selectionShown ? (
        <DiagramPanel diagram={diagram} selection={selectionShown} properties={properties} onSelect={setSelection} />
      ) : null}
    </div>
  );
}

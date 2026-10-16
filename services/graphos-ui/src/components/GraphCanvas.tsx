import { ActionIcon, Group } from "@mantine/core";
import { IconFocusCentered, IconMap, IconMinus, IconPlus } from "@tabler/icons-react";
import { useEffect, useRef, useState, type MutableRefObject } from "react";
import { useTranslation } from "react-i18next";
import { EdgeCurvedArrowProgram } from "@sigma/edge-curve";
import { createNodeImageProgram } from "@sigma/node-image";
import Sigma from "sigma";
import {
  createNodeCompoundProgram,
  NodeCircleProgram,
  type NodeHoverDrawingFunction,
  type EdgeProgramType,
  type NodeProgramType,
} from "sigma/rendering";
import type { MouseCoords } from "sigma/types";
import { edgeDisplay, nodeDisplay, type Emphasis } from "../graph/canvasStyle";
import type { EdgeAttributes, GraphModel, NodeAttributes } from "../graph/GraphModel";
import { parallelCurvature } from "../graph/parallelEdges";
import type { Palette } from "../theme/tokens";
import { enableGestures, enableMinimap } from "./canvasGestures";
import classes from "./GraphCanvas.module.css";

export interface GraphCanvasProps {
  model: GraphModel;
  emphasis: Emphasis;
  palette: Palette;
  /** A click on a node or the empty canvas; `additive` with Ctrl, ⌘ or Shift held. */
  onSelect: (key: string | null, additive?: boolean) => void;
  /** Double-click on a node; e.g. to add its neighbours. */
  onExpand?: (key: string) => void;
  /** Click on an edge; edges are clickable only when this is set. */
  onSelectEdge?: (key: string) => void;
  /** Moves the camera to show these nodes whenever `token` changes. */
  frame?: { keys: string[]; token: number };
  /** How nodes and edges are drawn; the graph canvas style by default (the map has its own). */
  style?: { node: typeof nodeDisplay; edge: typeof edgeDisplay };
  /** Set to the renderer while it lives, e.g. to save the canvas as a picture. */
  rendererRef?: MutableRefObject<Sigma<NodeAttributes, EdgeAttributes> | null>;
  /** Shift-drag on the empty canvas selects the nodes inside the loop drawn. */
  onLasso?: (keys: string[], additive: boolean) => void;
  /** Double-click on the empty canvas, at that point (graph coordinates). */
  onCreateAt?: (point: { x: number; y: number }) => void;
  /** Alt-drag from a node onto another. */
  onLink?: (src: string, dst: string) => void;
  /** Offers the overview map of the whole canvas (shown from a few dozen nodes). */
  minimap?: boolean;
}

const GRAPH_STYLE = { node: nodeDisplay, edge: edgeDisplay };
/** A node of the base size drawn smaller than this many pixels: the canvas is "far". */
const FAR_PIXELS = 4;
const BASE_NODE_SIZE = 5;
/** Below this many nodes the whole canvas fits the view: no minimap. */
const MINIMAP_MIN_NODES = 40;

/**
 * Draws a `GraphModel` with sigma (WebGL) and reports clicks. Nodes can be
 * dragged; the camera has zoom controls. Colours and labels come from
 * `canvasStyle`, so this component only connects sigma to React.
 */
export function GraphCanvas({
  model,
  emphasis,
  palette,
  onSelect,
  onExpand,
  onSelectEdge,
  frame,
  style = GRAPH_STYLE,
  rendererRef,
  onLasso,
  onCreateAt,
  onLink,
  minimap = false,
}: GraphCanvasProps) {
  const { t } = useTranslation();
  const container = useRef<HTMLDivElement>(null);
  const overlay = useRef<HTMLCanvasElement>(null);
  const minimapCanvas = useRef<HTMLCanvasElement>(null);
  const [minimapOn, setMinimapOn] = useState(true);
  const [manyNodes, setManyNodes] = useState(false);
  const sigma = useRef<Sigma<NodeAttributes, EdgeAttributes> | null>(null);
  // Sigma's callbacks read these refs, so they always see the latest props.
  const latest = useRef({ emphasis, palette, onSelect, onExpand, onSelectEdge, style, onLasso, onCreateAt, onLink });
  latest.current = { emphasis, palette, onSelect, onExpand, onSelectEdge, style, onLasso, onCreateAt, onLink };
  // What the reducers add: the edge under the pointer, and whether the camera is far out.
  const view = useRef<{ hoveredEdge: string | null; far: boolean }>({ hoveredEdge: null, far: false });
  // Set just after a lasso or a link ends: the click that follows the release is not a click.
  const drew = useRef(false);
  const edgesClickable = Boolean(onSelectEdge);

  useEffect(() => {
    if (!container.current) return;
    const renderer = new Sigma<NodeAttributes, EdgeAttributes>(model.graph, container.current, {
      defaultEdgeType: "arrow",
      zIndex: true,
      labelRenderedSizeThreshold: Number.POSITIVE_INFINITY,
      labelFont: "-apple-system, BlinkMacSystemFont, Inter, sans-serif",
      labelSize: 11,
      labelWeight: "500",
      stagePadding: 80,
      allowInvalidContainer: true,
      enableEdgeEvents: edgesClickable,
      nodeReducer: (key, node) => ({
        ...node,
        ...latest.current.style.node(
          key,
          node,
          { ...latest.current.emphasis, far: view.current.far },
          latest.current.palette,
        ),
      }),
      edgeReducer: (key, edge) => {
        // Edges sharing their two nodes bend apart, so each can be seen and clicked.
        const curvature = parallelCurvature(model.graph, key);
        const display = latest.current.style.edge(key, edge, latest.current.emphasis, latest.current.palette);
        // The edge under the pointer says what it is, whatever its ends show.
        const hovered =
          key === view.current.hoveredEdge && !edge.more
            ? {
                label: display.label ? `${edge.edgeType} · ${display.label}` : edge.edgeType,
                forceLabel: true,
                zIndex: 3,
              }
            : {};
        return {
          ...edge,
          ...display,
          ...hovered,
          ...(curvature === null ? {} : { type: "curvedArrow", curvature }),
        };
      },
      labelColor: { color: palette.text },
      defaultDrawNodeHover: drawHoverLabel(() => latest.current.palette),
      // A label's icon drawn on its disc (label styles); edge captions on the focus only.
      nodeProgramClasses: {
        icon: iconProgram(),
      },
      edgeProgramClasses: {
        curvedArrow: EdgeCurvedArrowProgram as unknown as EdgeProgramType<NodeAttributes, EdgeAttributes>,
      },
      renderEdgeLabels: true,
      edgeLabelColor: { color: palette.muted },
      edgeLabelSize: 10,
      edgeLabelFont: "-apple-system, BlinkMacSystemFont, Inter, sans-serif",
    });
    sigma.current = renderer;
    if (rendererRef) rendererRef.current = renderer;

    // A "+N more" placeholder is not a node to inspect: a click opens the next page.
    renderer.on("clickNode", ({ node, event }) => {
      if (drew.current) return;
      if (model.graph.getNodeAttribute(node, "more") !== undefined) latest.current.onExpand?.(node);
      else latest.current.onSelect(node, isAdditive(event));
    });
    renderer.on("clickStage", ({ event }) => {
      if (!drew.current && !isAdditive(event)) latest.current.onSelect(null);
    });
    renderer.on("doubleClickStage", (event) => {
      const create = latest.current.onCreateAt;
      if (!create) return;
      event.preventSigmaDefault();
      create(renderer.viewportToGraph(event.event));
    });
    renderer.on("clickEdge", ({ edge }) => latest.current.onSelectEdge?.(edge));
    // A pointer over clickable things, as for links.
    const pointer = (on: boolean) => () => {
      if (container.current) container.current.style.cursor = on ? "pointer" : "";
    };
    renderer.on("enterNode", pointer(true));
    renderer.on("leaveNode", pointer(false));
    const hoverEdge = (edge: string | null) => {
      const previous = view.current.hoveredEdge;
      view.current.hoveredEdge = edge;
      const edges = [previous, edge].filter((key): key is string => key !== null && model.graph.hasEdge(key));
      renderer.refresh({ partialGraph: { edges }, skipIndexation: true });
    };
    renderer.on("enterEdge", ({ edge }) => {
      pointer(true)();
      hoverEdge(edge);
    });
    renderer.on("leaveEdge", () => {
      pointer(false)();
      hoverEdge(null);
    });
    // Far out, pictograms and hub labels are left out; redrawn only when that changes.
    const camera = renderer.getCamera();
    const onCamera = () => {
      const far = renderer.scaleSize(BASE_NODE_SIZE) < FAR_PIXELS;
      if (far !== view.current.far) {
        view.current.far = far;
        renderer.refresh();
      }
    };
    camera.on("updated", onCamera);
    const countNodes = () => setManyNodes(model.graph.order >= MINIMAP_MIN_NODES);
    countNodes();
    const unsubscribe = model.subscribe(countNodes);
    renderer.on("doubleClickNode", (event) => {
      event.preventSigmaDefault();
      latest.current.onExpand?.(event.node);
    });
    const stopGestures = overlay.current
      ? enableGestures(
          renderer,
          model,
          overlay.current,
          () => latest.current,
          () => latest.current.palette,
          drew,
        )
      : () => undefined;
    const stopMinimap = minimapCanvas.current
      ? enableMinimap(renderer, minimapCanvas.current, () => latest.current.palette)
      : () => undefined;
    return () => {
      stopGestures();
      stopMinimap();
      unsubscribe();
      camera.off("updated", onCamera);
      renderer.kill();
      sigma.current = null;
      if (rendererRef) rendererRef.current = null;
    };
  }, [model, edgesClickable]);

  // The minimap draws after a render: one is due when it appears.
  useEffect(() => {
    if (minimapOn && manyNodes) sigma.current?.scheduleRender();
  }, [minimapOn, manyNodes]);

  // New colours or emphasis: redraw without rebuilding the renderer.
  useEffect(() => {
    sigma.current?.setSetting("labelColor", { color: palette.text });
    sigma.current?.setSetting("edgeLabelColor", { color: palette.muted });
    sigma.current?.refresh();
  }, [emphasis, palette, style]);

  // Frame the given nodes, once they are drawn (the graph's extent may have just changed).
  useEffect(() => {
    if (!frame || frame.keys.length === 0) return;
    const id = requestAnimationFrame(() => {
      const renderer = sigma.current;
      if (renderer) frameNodes(renderer, frame.keys);
    });
    return () => cancelAnimationFrame(id);
  }, [frame]);

  const camera = () => sigma.current?.getCamera();
  return (
    <div className={classes.root} style={{ background: palette.canvas }}>
      <div ref={container} className={classes.stage} data-testid="graph-canvas" />
      <canvas ref={overlay} className={classes.overlay} aria-hidden />
      {minimap ? (
        <canvas
          ref={minimapCanvas}
          className={classes.minimap}
          hidden={!minimapOn || !manyNodes}
          aria-label={t("components.graphCanvas.minimap")}
          style={{ background: palette.canvas }}
        />
      ) : null}
      <Group gap={2} className={classes.zoom}>
        {minimap && manyNodes ? (
          <ActionIcon
            variant={minimapOn ? "light" : "subtle"}
            color="gray"
            aria-label={t("components.graphCanvas.toggleMinimap")}
            aria-pressed={minimapOn}
            onClick={() => setMinimapOn((on) => !on)}
          >
            <IconMap size={15} />
          </ActionIcon>
        ) : null}
        <ActionIcon
          variant="subtle"
          color="gray"
          aria-label={t("components.graphCanvas.zoomOut")}
          onClick={() => camera()?.animatedUnzoom(1.5)}
        >
          <IconMinus size={15} />
        </ActionIcon>
        <ActionIcon
          variant="subtle"
          color="gray"
          aria-label={t("components.graphCanvas.showAll")}
          onClick={() => camera()?.animatedReset()}
        >
          <IconFocusCentered size={15} />
        </ActionIcon>
        <ActionIcon
          variant="subtle"
          color="gray"
          aria-label={t("components.graphCanvas.zoomIn")}
          onClick={() => camera()?.animatedZoom(1.5)}
        >
          <IconPlus size={15} />
        </ActionIcon>
      </Group>
    </div>
  );
}

/**
 * The disc with its label's pictogram on it, in white. Made for each renderer:
 * the pictograms' texture is shared by the renderer's own programs.
 */
function iconProgram(): NodeProgramType<NodeAttributes, EdgeAttributes> {
  return createNodeCompoundProgram<NodeAttributes, EdgeAttributes>([
    NodeCircleProgram as unknown as NodeProgramType<NodeAttributes, EdgeAttributes>,
    createNodeImageProgram<NodeAttributes, EdgeAttributes>({
      drawingMode: "color",
      colorAttribute: "pictoColor",
      padding: 0.24,
      keepWithinCircle: true,
    }),
  ]);
}

/**
 * Animates the camera onto `keys`: centred on them, zoomed so they fill most
 * of the view, but never closer than a comfortable reading distance.
 */
function frameNodes(renderer: Sigma<NodeAttributes, EdgeAttributes>, keys: string[]): void {
  renderer.refresh();
  const points = keys.flatMap((key) => {
    const data = renderer.getNodeDisplayData(key);
    return data ? [data] : [];
  });
  if (points.length === 0) return;
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const [minX, maxX, minY, maxY] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  // Camera coordinates are those of the framed graph (0..1); ratio 1 shows all of it.
  const ratio = Math.min(1, Math.max(0.08, Math.max(maxX - minX, maxY - minY) * 1.6));
  void renderer.getCamera().animate({ x: (minX + maxX) / 2, y: (minY + maxY) / 2, ratio }, { duration: 450 });
}

/** Whether a click adds to the selection: Ctrl, ⌘ or Shift held. */
function isAdditive(event: MouseCoords): boolean {
  const original = event.original as MouseEvent;
  return Boolean(original.ctrlKey || original.metaKey || original.shiftKey);
}

/** The hover label: the node's name on a panel-coloured tag, in either theme. */
function drawHoverLabel(palette: () => Palette): NodeHoverDrawingFunction<NodeAttributes, EdgeAttributes> {
  return (context, display, settings) => {
    // The reducer adds the uncut name; hover shows all of it.
    const data = { ...display, label: (display as { fullLabel?: string }).fullLabel ?? display.label };
    if (!data.label) return;
    const size = settings.labelSize;
    context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`;
    const width = context.measureText(data.label).width;
    const x = data.x + data.size + 4;
    const y = data.y - size / 2 - 4;
    context.fillStyle = palette().panel;
    context.beginPath();
    context.roundRect(x - 4, y, width + 8, size + 8, 5);
    context.fill();
    context.fillStyle = palette().text;
    context.fillText(data.label, x, data.y + size / 3);
  };
}

import { ActionIcon, Group } from "@mantine/core";
import { IconFocusCentered, IconMinus, IconPlus } from "@tabler/icons-react";
import { useEffect, useRef, type MutableRefObject } from "react";
import { useTranslation } from "react-i18next";
import Sigma from "sigma";
import type { NodeHoverDrawingFunction } from "sigma/rendering";
import type { MouseCoords } from "sigma/types";
import { edgeDisplay, nodeDisplay, type Emphasis } from "../graph/canvasStyle";
import type { EdgeAttributes, GraphModel, NodeAttributes } from "../graph/GraphModel";
import type { Palette } from "../theme/tokens";
import classes from "./GraphCanvas.module.css";

export interface GraphCanvasProps {
  model: GraphModel;
  emphasis: Emphasis;
  palette: Palette;
  onSelect: (key: string | null) => void;
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
}

const GRAPH_STYLE = { node: nodeDisplay, edge: edgeDisplay };

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
}: GraphCanvasProps) {
  const { t } = useTranslation();
  const container = useRef<HTMLDivElement>(null);
  const sigma = useRef<Sigma<NodeAttributes, EdgeAttributes> | null>(null);
  // Sigma's callbacks read these refs, so they always see the latest props.
  const latest = useRef({ emphasis, palette, onSelect, onExpand, onSelectEdge, style });
  latest.current = { emphasis, palette, onSelect, onExpand, onSelectEdge, style };
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
        ...latest.current.style.node(key, node, latest.current.emphasis, latest.current.palette),
      }),
      edgeReducer: (key, edge) => ({
        ...edge,
        ...latest.current.style.edge(key, edge, latest.current.emphasis, latest.current.palette),
      }),
      labelColor: { color: palette.text },
      defaultDrawNodeHover: drawHoverLabel(() => latest.current.palette),
    });
    sigma.current = renderer;
    if (rendererRef) rendererRef.current = renderer;

    // A "+N more" placeholder is not a node to inspect: a click opens the next page.
    renderer.on("clickNode", ({ node }) => {
      if (model.graph.getNodeAttribute(node, "more") !== undefined) latest.current.onExpand?.(node);
      else latest.current.onSelect(node);
    });
    renderer.on("clickStage", () => latest.current.onSelect(null));
    renderer.on("clickEdge", ({ edge }) => latest.current.onSelectEdge?.(edge));
    // A pointer over clickable things, as for links.
    const pointer = (on: boolean) => () => {
      if (container.current) container.current.style.cursor = on ? "pointer" : "";
    };
    renderer.on("enterNode", pointer(true));
    renderer.on("leaveNode", pointer(false));
    renderer.on("enterEdge", pointer(true));
    renderer.on("leaveEdge", pointer(false));
    renderer.on("doubleClickNode", (event) => {
      event.preventSigmaDefault();
      latest.current.onExpand?.(event.node);
    });
    const stopDragging = enableDragging(renderer, model);
    return () => {
      stopDragging();
      renderer.kill();
      sigma.current = null;
      if (rendererRef) rendererRef.current = null;
    };
  }, [model, edgesClickable]);

  // New colours or emphasis: redraw without rebuilding the renderer.
  useEffect(() => {
    sigma.current?.setSetting("labelColor", { color: palette.text });
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
      <Group gap={2} className={classes.zoom}>
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

/** Lets the user drag nodes; returns a function that removes the handlers. */
function enableDragging(renderer: Sigma<NodeAttributes, EdgeAttributes>, model: GraphModel): () => void {
  let dragged: string | null = null;
  renderer.on("downNode", ({ node }) => {
    dragged = node;
    renderer.getCamera().disable();
  });
  const captor = renderer.getMouseCaptor();
  const move = (event: MouseCoords) => {
    if (!dragged) return;
    const position = renderer.viewportToGraph(event);
    model.graph.mergeNodeAttributes(dragged, { x: position.x, y: position.y });
    event.preventSigmaDefault();
    event.original.preventDefault();
    event.original.stopPropagation();
  };
  const release = () => {
    dragged = null;
    renderer.getCamera().enable();
  };
  captor.on("mousemovebody", move);
  captor.on("mouseup", release);
  return () => {
    captor.off("mousemovebody", move);
    captor.off("mouseup", release);
  };
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

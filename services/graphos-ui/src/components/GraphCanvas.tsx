import { ActionIcon, Group } from "@mantine/core";
import { IconFocusCentered, IconMinus, IconPlus } from "@tabler/icons-react";
import { useEffect, useRef } from "react";
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
}

/**
 * Draws a `GraphModel` with sigma (WebGL) and reports clicks. Nodes can be
 * dragged; the camera has zoom controls. Colours and labels come from
 * `canvasStyle`, so this component only connects sigma to React.
 */
export function GraphCanvas({ model, emphasis, palette, onSelect, onExpand }: GraphCanvasProps) {
  const container = useRef<HTMLDivElement>(null);
  const sigma = useRef<Sigma<NodeAttributes, EdgeAttributes> | null>(null);
  // Sigma's callbacks read these refs, so they always see the latest props.
  const latest = useRef({ emphasis, palette, onSelect, onExpand });
  latest.current = { emphasis, palette, onSelect, onExpand };

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
      nodeReducer: (key, node) => ({
        ...node,
        ...nodeDisplay(key, node, latest.current.emphasis, latest.current.palette),
      }),
      edgeReducer: (key, edge) => ({
        ...edge,
        ...edgeDisplay(key, edge, latest.current.emphasis, latest.current.palette),
      }),
      labelColor: { color: palette.text },
      defaultDrawNodeHover: drawHoverLabel(() => latest.current.palette),
    });
    sigma.current = renderer;

    renderer.on("clickNode", ({ node }) => latest.current.onSelect(node));
    renderer.on("clickStage", () => latest.current.onSelect(null));
    renderer.on("doubleClickNode", (event) => {
      event.preventSigmaDefault();
      latest.current.onExpand?.(event.node);
    });
    const stopDragging = enableDragging(renderer, model);
    return () => {
      stopDragging();
      renderer.kill();
      sigma.current = null;
    };
  }, [model]);

  // New colours or emphasis: redraw without rebuilding the renderer.
  useEffect(() => {
    sigma.current?.setSetting("labelColor", { color: palette.text });
    sigma.current?.refresh();
  }, [emphasis, palette]);

  const camera = () => sigma.current?.getCamera();
  return (
    <div className={classes.root} style={{ background: palette.canvas }}>
      <div ref={container} className={classes.stage} data-testid="graph-canvas" />
      <Group gap={2} className={classes.zoom}>
        <ActionIcon variant="subtle" color="gray" aria-label="Zoom out" onClick={() => camera()?.animatedUnzoom(1.5)}>
          <IconMinus size={15} />
        </ActionIcon>
        <ActionIcon
          variant="subtle"
          color="gray"
          aria-label="Show everything"
          onClick={() => camera()?.animatedReset()}
        >
          <IconFocusCentered size={15} />
        </ActionIcon>
        <ActionIcon variant="subtle" color="gray" aria-label="Zoom in" onClick={() => camera()?.animatedZoom(1.5)}>
          <IconPlus size={15} />
        </ActionIcon>
      </Group>
    </div>
  );
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
  return (context, data, settings) => {
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

import type Sigma from "sigma";
import type { MouseCoords } from "sigma/types";
import type { EdgeAttributes, GraphModel, NodeAttributes } from "../graph/GraphModel";
import { insidePolygon, type Point } from "../graph/selection";
import type { Palette } from "../theme/tokens";

type Renderer = Sigma<NodeAttributes, EdgeAttributes>;

export interface GestureHandlers {
  /** Shift-drag on the empty canvas: the nodes inside the drawn loop; `additive` with Ctrl or ⌘ too. */
  onLasso?: (keys: string[], additive: boolean) => void;
  /** Alt-drag from one node to another. */
  onLink?: (src: string, dst: string) => void;
}

function modifiers(event: MouseCoords): { shift: boolean; alt: boolean; mod: boolean } {
  const original = event.original as MouseEvent;
  return {
    shift: Boolean(original.shiftKey),
    alt: Boolean(original.altKey),
    mod: original.ctrlKey || original.metaKey,
  };
}

/** Sizes the overlay to its box at the screen's pixel density, cleared; returns its context. */
function clearOverlay(overlay: HTMLCanvasElement): CanvasRenderingContext2D | null {
  const ratio = window.devicePixelRatio || 1;
  const width = overlay.clientWidth;
  const height = overlay.clientHeight;
  if (overlay.width !== width * ratio || overlay.height !== height * ratio) {
    overlay.width = width * ratio;
    overlay.height = height * ratio;
  }
  const context = overlay.getContext("2d");
  if (!context) return null;
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  return context;
}

/** The nodes (placeholders and hidden nodes aside) drawn inside the loop, in viewport coordinates. */
export function nodesInside(renderer: Renderer, model: GraphModel, loop: Point[]): string[] {
  if (loop.length < 3) return [];
  const keys: string[] = [];
  model.graph.forEachNode((key, node) => {
    if (node.more !== undefined || renderer.getNodeDisplayData(key)?.hidden) return;
    if (insidePolygon(renderer.graphToViewport({ x: node.x, y: node.y }), loop)) keys.push(key);
  });
  return keys;
}

/**
 * Dragging a node moves it; Alt-dragging one draws a link to another node;
 * Shift-dragging (from a node or the empty canvas) draws a loop that selects
 * what it encloses. The overlay canvas shows the loop or the link while the
 * pointer moves. `drew` is set for the moment after a loop or a link ends, so
 * the click that follows the release is not taken as a click. Returns a
 * function that removes the handlers.
 */
export function enableGestures(
  renderer: Renderer,
  model: GraphModel,
  overlay: HTMLCanvasElement,
  handlers: () => GestureHandlers,
  palette: () => Palette,
  drew: { current: boolean },
): () => void {
  let dragged: string | null = null;
  let linkFrom: string | null = null;
  let hovered: string | null = null;
  let loop: Point[] | null = null;
  let additive = false;
  const camera = renderer.getCamera();

  renderer.on("enterNode", ({ node }) => {
    hovered = node;
  });
  renderer.on("leaveNode", () => {
    hovered = null;
  });
  const startLoop = (event: MouseCoords): boolean => {
    const keys = modifiers(event);
    if (!keys.shift || !handlers().onLasso) return false;
    camera.disable();
    loop = [{ x: event.x, y: event.y }];
    additive = keys.mod;
    return true;
  };
  renderer.on("downNode", ({ node, event }) => {
    if (startLoop(event)) return;
    camera.disable();
    if (modifiers(event).alt && handlers().onLink && model.graph.getNodeAttribute(node, "more") === undefined) {
      linkFrom = node;
    } else dragged = node;
  });
  renderer.on("downStage", ({ event }) => {
    startLoop(event);
  });

  const drawLoop = (points: Point[]) => {
    const context = clearOverlay(overlay);
    if (!context || points.length < 2) return;
    context.beginPath();
    context.moveTo(points[0]!.x, points[0]!.y);
    for (const point of points.slice(1)) context.lineTo(point.x, point.y);
    context.closePath();
    context.fillStyle = `${palette().accent}22`;
    context.strokeStyle = palette().accent;
    context.lineWidth = 1.5;
    context.setLineDash([5, 4]);
    context.fill();
    context.stroke();
  };
  const drawLink = (from: string, to: Point) => {
    const context = clearOverlay(overlay);
    const node = model.graph.getNodeAttributes(from);
    if (!context) return;
    const start = renderer.graphToViewport({ x: node.x, y: node.y });
    context.beginPath();
    context.moveTo(start.x, start.y);
    context.lineTo(to.x, to.y);
    context.strokeStyle = palette().accent;
    context.lineWidth = 2;
    context.stroke();
  };

  const captor = renderer.getMouseCaptor();
  const move = (event: MouseCoords) => {
    if (loop) {
      loop.push({ x: event.x, y: event.y });
      drawLoop(loop);
    } else if (linkFrom) drawLink(linkFrom, { x: event.x, y: event.y });
    else if (dragged) model.graph.mergeNodeAttributes(dragged, renderer.viewportToGraph(event));
    else return;
    event.preventSigmaDefault();
    event.original.preventDefault();
    event.original.stopPropagation();
  };
  const release = () => {
    const drawnLoop = loop && extent(loop) >= LOOP_MIN_PIXELS ? loop : null;
    if (drawnLoop) handlers().onLasso?.(nodesInside(renderer, model, drawnLoop), additive);
    if (linkFrom && hovered && hovered !== linkFrom && model.graph.getNodeAttribute(hovered, "more") === undefined) {
      handlers().onLink?.(linkFrom, hovered);
    }
    if (drawnLoop || linkFrom) {
      drew.current = true;
      window.setTimeout(() => (drew.current = false), 0);
    }
    if (loop || linkFrom) clearOverlay(overlay);
    dragged = null;
    linkFrom = null;
    loop = null;
    camera.enable();
  };
  captor.on("mousemovebody", move);
  captor.on("mouseup", release);
  return () => {
    captor.off("mousemovebody", move);
    captor.off("mouseup", release);
  };
}

/** How wide or tall a drawn loop must be to select anything: less is a Shift-click. */
const LOOP_MIN_PIXELS = 8;

/** The larger side of the box around the points. */
function extent(points: Point[]): number {
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  return Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
}

/** Pixels around the minimap's drawing. */
const MINIMAP_PAD = 6;

/**
 * Draws every node as a dot on a small canvas, with the part the camera shows
 * as a frame; a click or drag on it moves the camera there. Redrawn at most
 * ten times a second. Returns a function that removes the handlers.
 */
export function enableMinimap(renderer: Renderer, minimap: HTMLCanvasElement, palette: () => Palette): () => void {
  let bounds = { minX: 0, maxY: 0, scale: 1, left: MINIMAP_PAD, top: MINIMAP_PAD };
  let last = 0;
  let pending = 0;
  let stopped = false;

  const draw = () => {
    pending = 0;
    last = performance.now();
    // Hidden (or the canvas is gone): nothing to draw.
    if (stopped || minimap.clientWidth === 0) return;
    const context = clearOverlay(minimap);
    if (!context) return;
    const graph = renderer.getGraph();
    let [minX, maxX, minY, maxY] = [Infinity, -Infinity, Infinity, -Infinity];
    graph.forEachNode((_, node) => {
      minX = Math.min(minX, node.x);
      maxX = Math.max(maxX, node.x);
      minY = Math.min(minY, node.y);
      maxY = Math.max(maxY, node.y);
    });
    if (!Number.isFinite(minX)) return;
    const width = minimap.clientWidth - 2 * MINIMAP_PAD;
    const height = minimap.clientHeight - 2 * MINIMAP_PAD;
    const scale = Math.min(width / Math.max(maxX - minX, 1e-9), height / Math.max(maxY - minY, 1e-9));
    // Centred in the minimap.
    const left = MINIMAP_PAD + (width - (maxX - minX) * scale) / 2;
    const top = MINIMAP_PAD + (height - (maxY - minY) * scale) / 2;
    bounds = { minX, maxY, scale, left, top };
    const at = (x: number, y: number): [number, number] => [left + (x - minX) * scale, top + (maxY - y) * scale];
    graph.forEachNode((key, node) => {
      const display = renderer.getNodeDisplayData(key);
      if (!display || display.hidden) return;
      const [x, y] = at(node.x, node.y);
      context.fillStyle = display.color;
      context.fillRect(x - 1, y - 1, 2, 2);
    });
    const { width: viewWidth, height: viewHeight } = renderer.getDimensions();
    const corners = [
      renderer.viewportToGraph({ x: 0, y: 0 }),
      renderer.viewportToGraph({ x: viewWidth, y: viewHeight }),
    ];
    // The frame, kept inside the minimap when the camera shows more than the graph.
    const inside = (value: number, size: number) => Math.min(Math.max(value, 1), size - 1);
    const [ax, ay] = at(Math.min(corners[0]!.x, corners[1]!.x), Math.max(corners[0]!.y, corners[1]!.y));
    const [bx, by] = at(Math.max(corners[0]!.x, corners[1]!.x), Math.min(corners[0]!.y, corners[1]!.y));
    const [x0, x1] = [inside(ax, minimap.clientWidth), inside(bx, minimap.clientWidth)];
    const [y0, y1] = [inside(ay, minimap.clientHeight), inside(by, minimap.clientHeight)];
    context.strokeStyle = palette().accent;
    context.lineWidth = 1.5;
    context.strokeRect(x0, y0, x1 - x0, y1 - y0);
  };
  const schedule = () => {
    if (pending) return;
    pending = window.setTimeout(() => requestAnimationFrame(draw), Math.max(0, 100 - (performance.now() - last)));
  };

  const moveCamera = (event: PointerEvent) => {
    const box = minimap.getBoundingClientRect();
    const graphPoint = {
      x: bounds.minX + (event.clientX - box.left - bounds.left) / bounds.scale,
      y: bounds.maxY - (event.clientY - box.top - bounds.top) / bounds.scale,
    };
    const target = renderer.viewportToFramedGraph(renderer.graphToViewport(graphPoint));
    renderer.getCamera().setState({ x: target.x, y: target.y });
  };
  const down = (event: PointerEvent) => {
    minimap.setPointerCapture(event.pointerId);
    moveCamera(event);
  };
  const drag = (event: PointerEvent) => {
    if (minimap.hasPointerCapture(event.pointerId)) moveCamera(event);
  };

  renderer.on("afterRender", schedule);
  minimap.addEventListener("pointerdown", down);
  minimap.addEventListener("pointermove", drag);
  schedule();
  return () => {
    stopped = true;
    renderer.off("afterRender", schedule);
    minimap.removeEventListener("pointerdown", down);
    minimap.removeEventListener("pointermove", drag);
    window.clearTimeout(pending);
  };
}

/**
 * Runs `change` (e.g. adding a node outside the drawn extent) without the view
 * jumping: sigma fits the graph's extent to the screen, so a node beyond it
 * would rescale everything. The camera is then moved back so the nodes that
 * were there stay where they were drawn.
 */
export function holdView<T>(renderer: Renderer, change: () => T): T {
  const camera = renderer.getCamera();
  const {
    x: [x0, x1],
    y: [y0, y1],
  } = renderer.getBBox();
  const a = { x: x0, y: y0 };
  const b = { x: x1, y: y1 };
  const [va, vb] = [renderer.graphToViewport(a), renderer.graphToViewport(b)];
  const result = change();
  renderer.refresh();
  const [wa, wb] = [renderer.graphToViewport(a), renderer.graphToViewport(b)];
  const before = Math.hypot(vb.x - va.x, vb.y - va.y);
  const after = Math.hypot(wb.x - wa.x, wb.y - wa.y);
  if (before > 0 && after > 0) camera.setState({ ratio: (camera.ratio * after) / before });
  const now = renderer.viewportToFramedGraph(renderer.graphToViewport(a));
  const wanted = renderer.viewportToFramedGraph(va);
  camera.setState({ x: camera.x + now.x - wanted.x, y: camera.y + now.y - wanted.y });
  return result;
}

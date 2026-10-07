import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import type { EdgeKey } from "../api/graphosApi";
import type { GraphExport, GraphPath, Neighbor, Properties } from "../api/types";
import { t } from "../i18n";

/** What the canvas stores on each node. */
export interface NodeAttributes {
  id: string;
  /** The graphos label (`label` itself is sigma's display text). */
  vertexLabel: string;
  properties: Properties;
  x: number;
  y: number;
  size: number;
  /** Set on a "+N more" placeholder: how many neighbours are not shown yet. Not a graphos node. */
  more?: number;
}

/** What the canvas stores on each edge. */
export interface EdgeAttributes {
  /** The graphos edge type (`type` itself is sigma's edge style). */
  edgeType: string;
  properties: Properties;
  /** Set on the edge to a "+N more" placeholder. */
  more?: boolean;
}

/** A node as graphos names it: its own id within a label. */
export interface NodeRef {
  id: string;
  label: string;
}

/** One change to the canvas that Back can undo. */
interface Step {
  title: string;
  addedNodes: string[];
  addedEdges: string[];
  removedNodes: [string, NodeAttributes][];
  removedEdges: [string, string, string, EdgeAttributes][];
  /** Nodes changed in place (a "+N more" count), with their attributes before. */
  updatedNodes: [string, NodeAttributes][];
}

/** The node's key on the canvas; the same node always gets the same key. */
export function nodeKey(ref: NodeRef): string {
  return `${ref.label}:${ref.id}`;
}

/** The edge's key: graphos allows one edge per type between two nodes. */
export function edgeKey(type: string, srcKey: string, dstKey: string): string {
  return `${type}|${srcKey}|${dstKey}`;
}

/** The key of the "+N more" placeholder of a node's neighbours. */
export function moreKey(centerKey: string): string {
  return `more:${centerKey}`;
}

/** The text shown for a node: its `name`, else its id. */
export function displayName(properties: Properties, id: string): string {
  const name = properties.name;
  return typeof name === "string" && name ? name : id;
}

const BASE_SIZE = 5;
/** New neighbours per ring around the expanded node before the next, wider ring starts. */
const RING_CAPACITY = 12;
/** Steps Back can undo. */
const HISTORY_LIMIT = 50;

/** Which ring and which slot in it the `index`-th node of a fan takes; rings grow by 6 slots. */
function ringSlot(index: number): [number, number] {
  let ring = 0;
  let rest = index;
  while (rest >= RING_CAPACITY + ring * 6) {
    rest -= RING_CAPACITY + ring * 6;
    ring += 1;
  }
  return [ring, rest];
}

/**
 * The part of the tenant graph on the canvas. Adds what searches, expands
 * and paths return, placing new nodes next to the node they came from, and
 * notifies listeners after each change. Every change is recorded so Back
 * can undo it, and the neighbours each expand added are remembered so it
 * can be collapsed again. Knows nothing about React or sigma.
 */
export class GraphModel {
  readonly graph = new Graph<NodeAttributes, EdgeAttributes>({ type: "directed", multi: false });
  private readonly listeners = new Set<() => void>();
  private changes = 0;
  private readonly steps: Step[] = [];
  private current: Step | null = null;
  /** For each expanded node, the nodes its expansions added. */
  private readonly expandedFrom = new Map<string, Set<string>>();
  /** For each node new nodes were placed around: the fan's direction and the next free slot. */
  private readonly fans = new Map<string, { facing: number; span: number; next: number; step: number }>();

  /** Grows with every change; a cheap snapshot for React's external-store hook. */
  get revision(): number {
    return this.changes;
  }

  /** Calls `listener` after every change; returns the unsubscribe function. */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  get isEmpty(): boolean {
    return this.graph.order === 0;
  }

  /** What Back would undo, or null when there is nothing to undo. */
  get lastStep(): string | null {
    return this.steps.at(-1)?.title ?? null;
  }

  /** Adds a node (or refreshes its properties) and returns its key. */
  addNode(ref: NodeRef, properties: Properties, near?: string): string {
    return this.track(t("views.explore.history.open", { name: displayName(properties, ref.id) }), () =>
      this.putNode(ref, properties, near),
    );
  }

  /**
   * Replaces the canvas with an exported graph, nodes spread on a disc for
   * the layout to start from. Returns how many nodes and edges were drawn.
   * Starts a new history.
   */
  load(exported: GraphExport): { nodes: number; edges: number } {
    this.reset();
    const keyOf = new Map<string, string>();
    const radius = Math.sqrt(exported.nodes.length) * 2 + 1;
    exported.nodes.forEach((node, i) => {
      const { _kind: label, label: _display, ...properties } = node.attributes;
      const ref = { id: node.key, label };
      const key = nodeKey(ref);
      if (this.graph.hasNode(key)) return;
      // Sunflower spiral: an even spread with no overlaps to untangle first.
      const r = radius * Math.sqrt((i + 0.5) / exported.nodes.length);
      const angle = i * 2.399963;
      this.graph.addNode(key, {
        id: ref.id,
        vertexLabel: label,
        properties,
        size: BASE_SIZE,
        x: r * Math.cos(angle),
        y: r * Math.sin(angle),
      });
      if (!keyOf.has(node.key)) keyOf.set(node.key, key);
    });
    for (const edge of exported.edges) {
      const src = keyOf.get(edge.source);
      const dst = keyOf.get(edge.target);
      if (src && dst) this.putEdge(edge.attributes._type, src, dst, {});
    }
    this.resize();
    this.notify();
    return { nodes: this.graph.order, edges: this.graph.size };
  }

  /**
   * Adds neighbours of `centerKey` and the edges to them. New nodes fan out
   * on the side of the node away from what it already connects to, in rings,
   * so the rest of the canvas does not move. With `more` > 0 a "+N more"
   * placeholder stands for the neighbours not added yet. Returns the nodes
   * that were not on the canvas before.
   */
  addNeighbors(centerKey: string, neighbors: Neighbor[], more = 0): string[] {
    const name = this.graph.hasNode(centerKey)
      ? displayName(this.graph.getNodeAttribute(centerKey, "properties"), this.graph.getNodeAttribute(centerKey, "id"))
      : centerKey;
    return this.track(t("views.explore.history.expand", { name }), () => {
      const fresh = neighbors.filter(
        (n) => !this.graph.hasNode(nodeKey({ id: n.neighbor_id, label: n.neighbor_label })),
      );
      const spots = this.fan(centerKey, fresh.length + (more > 0 ? 1 : 0));
      const added: string[] = [];
      for (const n of neighbors) {
        const ref = { id: n.neighbor_id, label: n.neighbor_label };
        const isNew = !this.graph.hasNode(nodeKey(ref));
        const key = this.putNode(ref, n.neighbor_properties, undefined, isNew ? spots[added.length] : undefined);
        const [src, dst] = n.direction === "out" ? [centerKey, key] : [key, centerKey];
        this.putEdge(n.edge_type, src, dst, n.edge_properties);
        if (isNew) added.push(key);
      }
      const children = this.expandedFrom.get(centerKey) ?? new Set<string>();
      for (const key of added) children.add(key);
      this.expandedFrom.set(centerKey, children);
      this.setMore(centerKey, more, spots[added.length]);
      return added;
    });
  }

  /**
   * Adds a set of nodes and the edges among them as one step (e.g. a label's
   * best-connected nodes). They are laid out on their own and placed beside
   * what is already on the canvas, which does not move. Returns their keys.
   */
  addGroup(
    title: string,
    nodes: { ref: NodeRef; properties: Properties }[],
    edges: { type: string; src: NodeRef; dst: NodeRef; properties?: Properties }[],
  ): string[] {
    return this.track(title, () => {
      const spots = this.groupLayout(
        nodes.map((n) => nodeKey(n.ref)),
        edges,
      );
      const keys = nodes.map((n) => this.putNode(n.ref, n.properties, undefined, spots.get(nodeKey(n.ref))));
      for (const edge of edges) {
        const src = nodeKey(edge.src);
        const dst = nodeKey(edge.dst);
        if (this.graph.hasNode(src) && this.graph.hasNode(dst))
          this.putEdge(edge.type, src, dst, edge.properties ?? {});
      }
      return keys;
    });
  }

  /** Adds every node and edge of the paths; returns the keys on them. */
  addPaths(paths: GraphPath[]): { nodes: Set<string>; edges: Set<string> } {
    return this.track(t("views.explore.history.addPaths"), () => {
      const nodes = new Set<string>();
      const edges = new Set<string>();
      for (const path of paths) {
        let previous: string | undefined;
        const byId = new Map<string, string>();
        for (const node of path.nodes) {
          const key = this.putNode(node, node.properties ?? {}, previous);
          byId.set(node.id, key);
          nodes.add(key);
          previous = key;
        }
        for (const edge of path.edges) {
          const src = byId.get(edge.src_id);
          const dst = byId.get(edge.dst_id);
          if (src && dst) edges.add(this.putEdge(edge.type, src, dst, {}));
        }
      }
      this.relax();
      return { nodes, edges };
    });
  }

  /** Adds one edge between two nodes already on the canvas. */
  addEdge(type: string, srcKey: string, dstKey: string, properties: Properties = {}): string {
    return this.track(t("views.explore.history.connect", { type }), () =>
      this.putEdge(type, srcKey, dstKey, properties),
    );
  }

  /** Replaces a node's properties after it was edited. */
  updateNode(key: string, properties: Properties): void {
    if (!this.graph.hasNode(key)) return;
    this.graph.setNodeAttribute(key, "properties", properties);
    this.notify();
  }

  /** Takes a node off the canvas (it stays in graphos); Back puts it back. */
  removeNode(key: string): void {
    if (!this.graph.hasNode(key)) return;
    const attributes = this.graph.getNodeAttributes(key);
    this.track(t("views.explore.history.hide", { name: displayName(attributes.properties, attributes.id) }), () => {
      this.dropNode(key);
      this.dropNode(moreKey(key));
    });
  }

  /** Takes an edge off the canvas; Back puts it back. */
  removeEdge(key: string): void {
    if (!this.graph.hasEdge(key)) return;
    this.track(t("views.explore.history.hide", { name: this.graph.getEdgeAttribute(key, "edgeType") }), () =>
      this.dropEdge(key),
    );
  }

  /**
   * Takes a node or edge that was deleted in graphos off the canvas. Not
   * recorded: Back must not bring back something that no longer exists.
   */
  forget(key: string): void {
    if (this.graph.hasNode(key)) this.graph.dropNode(key);
    if (this.graph.hasNode(moreKey(key))) this.graph.dropNode(moreKey(key));
    if (this.graph.hasEdge(key)) this.graph.dropEdge(key);
    this.resize();
    this.notify();
  }

  /** Whether collapsing the node would take anything off the canvas. */
  canCollapse(key: string): boolean {
    return this.collapsible(key).length > 0;
  }

  /**
   * Takes back what expanding the node added: the neighbours it brought that
   * connect to nothing else on the canvas, and its "+N more" placeholder.
   */
  collapse(key: string): void {
    const doomed = this.collapsible(key);
    if (doomed.length === 0) return;
    const attributes = this.graph.getNodeAttributes(key);
    this.track(t("views.explore.history.collapse", { name: displayName(attributes.properties, attributes.id) }), () => {
      for (const child of doomed) this.dropNode(child);
      this.expandedFrom.delete(key);
    });
  }

  /** Undoes the last change: removes what it added and restores what it removed. */
  undo(): void {
    const step = this.steps.pop();
    if (!step) return;
    for (const key of step.addedEdges) if (this.graph.hasEdge(key)) this.graph.dropEdge(key);
    for (const key of step.addedNodes) if (this.graph.hasNode(key)) this.graph.dropNode(key);
    for (const [key, attributes] of step.updatedNodes) {
      if (this.graph.hasNode(key)) this.graph.replaceNodeAttributes(key, attributes);
    }
    for (const [key, attributes] of step.removedNodes) {
      if (!this.graph.hasNode(key)) this.graph.addNode(key, attributes);
    }
    for (const [key, src, dst, attributes] of step.removedEdges) {
      if (!this.graph.hasEdge(key) && this.graph.hasNode(src) && this.graph.hasNode(dst)) {
        this.graph.addDirectedEdgeWithKey(key, src, dst, attributes);
      }
    }
    this.resize();
    this.notify();
  }

  /** Empties the canvas and its history. */
  clear(): void {
    this.reset();
    this.notify();
  }

  /** graphos's identity of a canvas edge: its type and both end nodes. */
  edgeRef(key: string): EdgeKey {
    const [src, dst] = this.graph.extremities(key).map((end) => this.graph.getNodeAttributes(end));
    return {
      type: this.graph.getEdgeAttribute(key, "edgeType"),
      srcId: src!.id,
      srcLabel: src!.vertexLabel,
      dstId: dst!.id,
      dstLabel: dst!.vertexLabel,
    };
  }

  /** Replaces an edge's properties after it was edited. */
  updateEdge(key: string, properties: Properties): void {
    if (!this.graph.hasEdge(key)) return;
    this.graph.setEdgeAttribute(key, "properties", properties);
    this.notify();
  }

  /** The node and the nodes and edges touching it, for highlighting. */
  neighborhood(key: string): { nodes: Set<string>; edges: Set<string> } {
    if (!this.graph.hasNode(key)) return { nodes: new Set(), edges: new Set() };
    return {
      nodes: new Set([key, ...this.graph.neighbors(key)]),
      edges: new Set(this.graph.edges(key)),
    };
  }

  /** The `count` best-connected nodes on the canvas (placeholders aside), most connected first. */
  hubs(count: number): string[] {
    const ranked: [string, number][] = [];
    this.graph.forEachNode((key, node) => {
      if (node.more === undefined) ranked.push([key, this.graph.degree(key)]);
    });
    return ranked
      .filter(([, degree]) => degree > 1)
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, count)
      .map(([key]) => key);
  }

  /** Lets the layout settle a little after many nodes were added. */
  relax(iterations = 60): void {
    if (this.graph.order < 3) return;
    forceAtlas2.assign(this.graph, {
      iterations,
      settings: { ...forceAtlas2.inferSettings(this.graph), slowDown: 5, barnesHutOptimize: this.graph.order > 500 },
    });
  }

  /** Runs `change`, recording what it added and removed as one step of history, then notifies once. */
  private track<T>(title: string, change: () => T): T {
    const step: Step = {
      title,
      addedNodes: [],
      addedEdges: [],
      removedNodes: [],
      removedEdges: [],
      updatedNodes: [],
    };
    this.current = step;
    try {
      return change();
    } finally {
      this.current = null;
      const changed =
        step.addedNodes.length +
          step.addedEdges.length +
          step.removedNodes.length +
          step.removedEdges.length +
          step.updatedNodes.length >
        0;
      if (changed) {
        this.steps.push(step);
        if (this.steps.length > HISTORY_LIMIT) this.steps.shift();
      }
      this.resize();
      this.notify();
    }
  }

  private reset(): void {
    this.graph.clear();
    this.steps.length = 0;
    this.expandedFrom.clear();
    this.fans.clear();
  }

  private putNode(ref: NodeRef, properties: Properties, near?: string, position?: { x: number; y: number }): string {
    const key = nodeKey(ref);
    if (this.graph.hasNode(key)) {
      this.graph.mergeNodeAttributes(key, {
        properties: { ...this.graph.getNodeAttribute(key, "properties"), ...properties },
      });
      return key;
    }
    const spot = position ?? this.fan(near, 1)[0]!;
    this.graph.addNode(key, { id: ref.id, vertexLabel: ref.label, properties, size: BASE_SIZE, ...spot });
    this.current?.addedNodes.push(key);
    return key;
  }

  private putEdge(type: string, src: string, dst: string, properties: Properties, more = false): string {
    const key = edgeKey(type, src, dst);
    if (!this.graph.hasEdge(key)) {
      this.graph.addDirectedEdgeWithKey(key, src, dst, { edgeType: type, properties, ...(more ? { more } : {}) });
      this.current?.addedEdges.push(key);
    }
    return key;
  }

  /** Removes a node, recording it and its edges so Back can restore them. */
  private dropNode(key: string): void {
    if (!this.graph.hasNode(key)) return;
    this.graph.forEachEdge(key, (edge, attributes, src, dst) => {
      this.current?.removedEdges.push([edge, src, dst, { ...attributes }]);
    });
    this.current?.removedNodes.push([key, { ...this.graph.getNodeAttributes(key) }]);
    this.graph.dropNode(key);
  }

  private dropEdge(key: string): void {
    if (!this.graph.hasEdge(key)) return;
    const [src, dst] = this.graph.extremities(key);
    this.current?.removedEdges.push([key, src!, dst!, { ...this.graph.getEdgeAttributes(key) }]);
    this.graph.dropEdge(key);
  }

  /** Shows, updates or removes the "+N more" placeholder next to `centerKey`. */
  private setMore(centerKey: string, more: number, spot?: { x: number; y: number }): void {
    const key = moreKey(centerKey);
    if (more <= 0) {
      this.dropNode(key);
      return;
    }
    if (this.graph.hasNode(key)) {
      if (this.graph.getNodeAttribute(key, "more") === more) return;
      this.current?.updatedNodes.push([key, { ...this.graph.getNodeAttributes(key) }]);
      this.graph.mergeNodeAttributes(key, {
        more,
        properties: { name: t("views.explore.morePlaceholder", { count: more }) },
      });
      return;
    }
    const position = spot ?? this.fan(centerKey, 1)[0]!;
    this.graph.addNode(key, {
      id: "",
      vertexLabel: "",
      properties: { name: t("views.explore.morePlaceholder", { count: more }) },
      size: BASE_SIZE,
      more,
      ...position,
    });
    this.current?.addedNodes.push(key);
    this.putEdge("", centerKey, key, {}, true);
  }

  /** What collapsing `key` removes: its placeholder and the nodes it added that hang only on it. */
  private collapsible(key: string): string[] {
    const doomed: string[] = [];
    if (this.graph.hasNode(moreKey(key))) doomed.push(moreKey(key));
    for (const child of this.expandedFrom.get(key) ?? []) {
      if (!this.graph.hasNode(child)) continue;
      const others = this.graph.neighbors(child).filter((n) => n !== key && n !== moreKey(child));
      if (others.length === 0) doomed.push(child);
    }
    return doomed;
  }

  /**
   * Spots for `count` new nodes around `near`: on the side facing away from
   * the nodes it already connected to when first expanded (all around when
   * it had none), in rings one typical edge length apart. A node keeps its
   * fan, so a later page continues in the next free slots, outside the
   * earlier ones, instead of landing on top of them. Without `near`, around
   * the centre.
   */
  private fan(near: string | undefined, count: number): { x: number; y: number }[] {
    if (count === 0) return [];
    const hasCenter = near !== undefined && this.graph.hasNode(near);
    if (!hasCenter && this.graph.order === 0) return Array.from({ length: count }, () => ({ x: 0, y: 0 }));
    const origin = hasCenter
      ? { x: this.graph.getNodeAttribute(near, "x"), y: this.graph.getNodeAttribute(near, "y") }
      : { x: 0, y: 0 };
    const fan = (hasCenter && this.fans.get(near)) || this.newFan(hasCenter ? near : undefined, origin);
    if (hasCenter) this.fans.set(near, fan);
    const spots: { x: number; y: number }[] = [];
    for (let i = 0; i < count; i += 1) {
      const [ring, slot] = ringSlot(fan.next + i);
      const capacity = RING_CAPACITY + ring * 6;
      const radius = fan.step * (1 + ring * 0.6);
      const angle =
        fan.span >= 2 * Math.PI
          ? fan.facing + (2 * Math.PI * slot) / capacity + ring * 0.3
          : fan.facing - fan.span / 2 + (fan.span * (slot + 0.5)) / capacity;
      spots.push({ x: origin.x + radius * Math.cos(angle), y: origin.y + radius * Math.sin(angle) });
    }
    fan.next += count;
    return spots;
  }

  /**
   * Positions for a group of new nodes: a ForceAtlas2 layout of the group on
   * its own, scaled to the canvas's typical edge length, and set beside the
   * canvas's current extent (at the centre when the canvas is empty).
   */
  private groupLayout(keys: string[], edges: { src: NodeRef; dst: NodeRef }[]): Map<string, { x: number; y: number }> {
    const local = new Graph({ type: "directed", multi: false });
    keys.forEach((key, i) => {
      const angle = (2 * Math.PI * i) / Math.max(keys.length, 1);
      local.mergeNode(key, { x: Math.cos(angle), y: Math.sin(angle) });
    });
    for (const edge of edges) {
      const [src, dst] = [nodeKey(edge.src), nodeKey(edge.dst)];
      if (src !== dst && local.hasNode(src) && local.hasNode(dst) && !local.hasEdge(src, dst)) local.addEdge(src, dst);
    }
    if (local.order > 2) {
      forceAtlas2.assign(local, {
        iterations: 150,
        settings: { ...forceAtlas2.inferSettings(local), strongGravityMode: true, gravity: 1, scalingRatio: 10 },
      });
    }
    // Scale so neighbours sit about one typical edge apart; then move beside the canvas.
    const xs = local.mapNodes((_, a) => a.x as number);
    const ys = local.mapNodes((_, a) => a.y as number);
    const width = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 1e-6);
    const step = this.typicalEdgeLength();
    const scale = (step * Math.max(2, Math.sqrt(local.order) * 1.5)) / width;
    const cx = (Math.max(...xs) + Math.min(...xs)) / 2;
    const cy = (Math.max(...ys) + Math.min(...ys)) / 2;
    let offsetX = 0;
    let offsetY = 0;
    const existing = this.graph.filterNodes((key) => !local.hasNode(key));
    if (existing.length > 0) {
      const ex = existing.map((key) => this.graph.getNodeAttribute(key, "x"));
      const ey = existing.map((key) => this.graph.getNodeAttribute(key, "y"));
      offsetX = Math.max(...ex) + step * 2 + (width * scale) / 2;
      offsetY = (Math.max(...ey) + Math.min(...ey)) / 2;
    }
    const spots = new Map<string, { x: number; y: number }>();
    local.forEachNode((key, a) => {
      spots.set(key, { x: offsetX + ((a.x as number) - cx) * scale, y: offsetY + ((a.y as number) - cy) * scale });
    });
    return spots;
  }

  /** A fan facing away from what `near` connects to now (all around when nothing). */
  private newFan(
    near: string | undefined,
    origin: { x: number; y: number },
  ): { facing: number; span: number; next: number; step: number } {
    const step = this.typicalEdgeLength();
    const linked = near ? this.graph.neighbors(near).filter((n) => n !== moreKey(near)) : [];
    if (linked.length > 0) {
      const cx = linked.reduce((sum, n) => sum + this.graph.getNodeAttribute(n, "x"), 0) / linked.length;
      const cy = linked.reduce((sum, n) => sum + this.graph.getNodeAttribute(n, "y"), 0) / linked.length;
      if (Math.hypot(origin.x - cx, origin.y - cy) > step * 0.05) {
        return { facing: Math.atan2(origin.y - cy, origin.x - cx), span: Math.PI * 1.3, next: 0, step };
      }
    }
    return { facing: Math.random() * 2 * Math.PI, span: 2 * Math.PI, next: 0, step };
  }

  /** The median length of up to 200 edges on the canvas, so new nodes match its scale. */
  private typicalEdgeLength(): number {
    const lengths: number[] = [];
    this.graph.someEdge((_edge, _attributes, _src, _dst, a, b) => {
      lengths.push(Math.hypot(a.x - b.x, a.y - b.y));
      return lengths.length >= 200;
    });
    if (lengths.length === 0) return 1.5;
    lengths.sort((a, b) => a - b);
    return Math.max(lengths[Math.floor(lengths.length / 2)]!, 0.5);
  }

  /** Better-connected nodes are drawn a little larger; placeholders stay small. */
  private resize(): void {
    this.graph.forEachNode((key, node) => {
      const size = node.more !== undefined ? BASE_SIZE - 1 : BASE_SIZE + Math.min(6, Math.sqrt(this.graph.degree(key)));
      this.graph.setNodeAttribute(key, "size", size);
    });
  }

  private notify(): void {
    this.changes += 1;
    for (const listener of this.listeners) listener();
  }
}

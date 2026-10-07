import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import type { GraphExport, GraphPath, Neighbor, Properties } from "../api/types";

/** What the canvas stores on each node. */
export interface NodeAttributes {
  id: string;
  /** The graphos label (`label` itself is sigma's display text). */
  vertexLabel: string;
  properties: Properties;
  x: number;
  y: number;
  size: number;
}

/** What the canvas stores on each edge. */
export interface EdgeAttributes {
  /** The graphos edge type (`type` itself is sigma's edge style). */
  edgeType: string;
  properties: Properties;
}

/** A node as graphos names it: its own id within a label. */
export interface NodeRef {
  id: string;
  label: string;
}

/** The node's key on the canvas; the same node always gets the same key. */
export function nodeKey(ref: NodeRef): string {
  return `${ref.label}:${ref.id}`;
}

/** The edge's key: graphos allows one edge per type between two nodes. */
export function edgeKey(type: string, srcKey: string, dstKey: string): string {
  return `${type}|${srcKey}|${dstKey}`;
}

/** The text shown for a node: its `name`, else its id. */
export function displayName(properties: Properties, id: string): string {
  const name = properties.name;
  return typeof name === "string" && name ? name : id;
}

const BASE_SIZE = 5;

/**
 * The part of the tenant graph on the canvas. Adds what searches, expands
 * and paths return, placing new nodes next to the node they came from, and
 * notifies listeners after each change. Knows nothing about React or sigma.
 */
export class GraphModel {
  readonly graph = new Graph<NodeAttributes, EdgeAttributes>({ type: "directed", multi: false });
  private readonly listeners = new Set<() => void>();
  private changes = 0;

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

  /** Adds a node (or refreshes its properties) and returns its key. */
  addNode(ref: NodeRef, properties: Properties, near?: string): string {
    const key = this.putNode(ref, properties, near);
    this.notify();
    return key;
  }

  /**
   * Replaces the canvas with an exported graph, nodes spread on a disc for
   * the layout to start from. Returns how many nodes and edges were drawn.
   */
  load(exported: GraphExport): { nodes: number; edges: number } {
    this.graph.clear();
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

  /** Adds the neighbours of `centerKey` and the edges to them, in a ring around it. */
  addNeighbors(centerKey: string, neighbors: Neighbor[]): string[] {
    const added: string[] = [];
    neighbors.forEach((n, i) => {
      const key = this.putNode(
        { id: n.neighbor_id, label: n.neighbor_label },
        n.neighbor_properties,
        centerKey,
        i,
        neighbors.length,
      );
      const [src, dst] = n.direction === "out" ? [centerKey, key] : [key, centerKey];
      this.putEdge(n.edge_type, src, dst, n.edge_properties);
      added.push(key);
    });
    this.resize();
    this.notify();
    return added;
  }

  /** Adds every node and edge of the paths; returns the keys on them. */
  addPaths(paths: GraphPath[]): { nodes: Set<string>; edges: Set<string> } {
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
    this.notify();
    return { nodes, edges };
  }

  /** Adds one edge between two nodes already on the canvas. */
  addEdge(type: string, srcKey: string, dstKey: string, properties: Properties = {}): string {
    const key = this.putEdge(type, srcKey, dstKey, properties);
    this.resize();
    this.notify();
    return key;
  }

  /** Replaces a node's properties after it was edited. */
  updateNode(key: string, properties: Properties): void {
    if (!this.graph.hasNode(key)) return;
    this.graph.setNodeAttribute(key, "properties", properties);
    this.notify();
  }

  removeNode(key: string): void {
    if (this.graph.hasNode(key)) this.graph.dropNode(key);
    this.resize();
    this.notify();
  }

  removeEdge(key: string): void {
    if (this.graph.hasEdge(key)) this.graph.dropEdge(key);
    this.resize();
    this.notify();
  }

  /** Empties the canvas. */
  clear(): void {
    this.graph.clear();
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

  /** Lets the layout settle a little after many nodes were added. */
  relax(iterations = 60): void {
    if (this.graph.order < 3) return;
    forceAtlas2.assign(this.graph, {
      iterations,
      settings: { ...forceAtlas2.inferSettings(this.graph), slowDown: 5, barnesHutOptimize: this.graph.order > 500 },
    });
  }

  private putNode(ref: NodeRef, properties: Properties, near?: string, index = 0, total = 1): string {
    const key = nodeKey(ref);
    if (this.graph.hasNode(key)) {
      this.graph.mergeNodeAttributes(key, {
        properties: { ...this.graph.getNodeAttribute(key, "properties"), ...properties },
      });
      return key;
    }
    const position = this.positionNear(near, index, total);
    this.graph.addNode(key, { id: ref.id, vertexLabel: ref.label, properties, size: BASE_SIZE, ...position });
    return key;
  }

  private putEdge(type: string, src: string, dst: string, properties: Properties): string {
    const key = edgeKey(type, src, dst);
    if (!this.graph.hasEdge(key)) this.graph.addDirectedEdgeWithKey(key, src, dst, { edgeType: type, properties });
    return key;
  }

  /** A spot on a ring around `near` (or around the centre for a first node). */
  private positionNear(near: string | undefined, index: number, total: number): { x: number; y: number } {
    const origin =
      near && this.graph.hasNode(near)
        ? { x: this.graph.getNodeAttribute(near, "x"), y: this.graph.getNodeAttribute(near, "y") }
        : { x: 0, y: 0 };
    if (!near && this.graph.order === 0) return origin;
    const angle = (2 * Math.PI * index) / Math.max(total, 1) + Math.random() * 0.3;
    const radius = 1 + Math.sqrt(total) * 0.35;
    return { x: origin.x + radius * Math.cos(angle), y: origin.y + radius * Math.sin(angle) };
  }

  /** Better-connected nodes are drawn a little larger. */
  private resize(): void {
    this.graph.forEachNode((key) => {
      this.graph.setNodeAttribute(key, "size", BASE_SIZE + Math.min(6, Math.sqrt(this.graph.degree(key))));
    });
  }

  private notify(): void {
    this.changes += 1;
    for (const listener of this.listeners) listener();
  }
}

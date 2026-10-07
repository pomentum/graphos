/**
 * Shapes of the graphos REST API that the UI uses, as the server returns them.
 * Field names follow the API (snake_case); see the OpenAPI schema at /docs.
 */

export type Properties = Record<string, unknown>;
export type Direction = "out" | "in" | "both";

/** `GET /v1/me`: the signed-in token. */
export interface Me {
  consumer: string;
  admin: boolean;
  access: "read" | "write";
  tenants: string[];
}

export interface SearchIndexSpec {
  labels: string[];
  properties: string[];
  analyzer: "identifier" | "text";
  language: string;
}

/** `GET /v1/{tenant}/schema`: labels and search indexes of one tenant. */
export interface TenantSchema {
  name: string;
  vertex_labels: string[];
  edge_labels: string[];
  label_hierarchy: Record<string, string>;
  search_indexes: Record<string, SearchIndexSpec>;
  rule_edge_labels: string[];
}

/**
 * `GET …/graph/export`: nodes and the edges among them, in graphology's format.
 * Node keys are the nodes' own ids; `_kind` is the label, `_type` the edge type.
 */
export interface GraphExport {
  nodes: { key: string; attributes: Properties & { _kind: string } }[];
  edges: { key: string; source: string; target: string; attributes: Properties & { _type: string } }[];
}

/** `POST …/graph/nodes:scan`: one page of nodes in id order. */
export interface NodesScanResponse {
  items: { id: string; label: string; properties: Properties }[];
  next_cursor: string | null;
}

export interface SearchHit {
  /** AGE graphid; the node's own id is `properties.id`. */
  id: number;
  label: string;
  properties: Properties;
  score: number | null;
  matched: string | null;
}

export interface SearchResponse {
  query: string;
  index: string | null;
  truncated: boolean;
  nodes: SearchHit[];
}

/** One node with its revision; `etag` goes back as `If-Match` on a write. */
export interface NodeRecord {
  external_id: string;
  label: string;
  properties: Properties;
  rev: number;
  etag: string;
}

export interface Neighbor {
  neighbor_id: string;
  neighbor_label: string;
  neighbor_properties: Properties;
  direction: "in" | "out";
  edge_type: string;
  edge_properties: Properties;
}

export interface NeighborsResponse {
  node_id: string;
  truncated: boolean;
  neighbors: Neighbor[];
}

export interface NodeUpsert {
  id: string;
  label: string;
  properties: Properties;
}

export interface EdgeUpsert {
  src_id: string;
  src_label: string;
  dst_id: string;
  dst_label: string;
  type: string;
  properties: Properties;
}

export interface EdgesUpsertResponse {
  edges_upserted: number;
  edges_skipped: number;
}

export interface DeleteNodeResponse {
  nodes_deleted: number;
  edges_deleted: number;
}

export interface PathsRequest {
  src: string;
  dst: string;
  src_label?: string;
  dst_label?: string;
  rel_types?: string[];
  direction: Direction;
  max_hops: number;
  limit: number;
  include_properties: boolean;
}

export interface PathNode {
  id: string;
  label: string;
  properties: Properties | null;
}

export interface PathEdge {
  type: string;
  src_id: string;
  dst_id: string;
}

export interface GraphPath {
  hops: number;
  nodes: PathNode[];
  edges: PathEdge[];
}

export interface PathsResponse {
  hops: number | null;
  paths: GraphPath[];
  truncated: boolean;
}

export interface LabelCount {
  label: string;
  kind: "v" | "e";
  count: number;
  total_bytes: number;
}

export interface TenantStats {
  n_vertices: number;
  n_edges: number;
  total_bytes: number;
  labels: LabelCount[];
}

export interface HistogramResponse {
  label: string;
  property: string;
  n_distinct: number;
  n_total: number;
  bins: { value: string; count: number }[];
}

/** One record of the change feed: what changed and who changed it, not the new values. */
export interface ChangeItem {
  cursor: string;
  at: string;
  op: "upsert" | "merge" | "patch" | "delete" | "resolve" | "communities";
  kind: "node" | "edge";
  label: string | null;
  src_label: string | null;
  dst_label: string | null;
  ids: string[] | null;
  edges: [string, string][] | null;
  rev: number | null;
  count: number;
  consumer: string | null;
}

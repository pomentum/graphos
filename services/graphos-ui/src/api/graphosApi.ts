import type { HttpClient, Tagged } from "./http";
import type {
  DeleteNodeResponse,
  EdgeRecord,
  EdgeUpsert,
  GraphExport,
  EdgesUpsertResponse,
  HistogramResponse,
  Me,
  NeighborsResponse,
  NodeRecord,
  NodesScanRequest,
  NodesScanResponse,
  NodeUpsert,
  PathsRequest,
  PathsResponse,
  Properties,
  SearchResponse,
  TenantSchema,
  TenantStats,
} from "./types";

export interface SearchParams {
  q: string;
  index?: string;
  label?: string;
  limit?: number;
  fuzzy?: boolean;
}

export interface EdgeKey {
  type: string;
  srcId: string;
  srcLabel: string;
  dstId: string;
  dstLabel: string;
}

const enc = encodeURIComponent;

function edgeQuery(edge: EdgeKey) {
  return { src: edge.srcId, dst: edge.dstId, src_label: edge.srcLabel, dst_label: edge.dstLabel };
}

/** The calling token, independent of any tenant. */
export function fetchMe(http: HttpClient): Promise<Me> {
  return http.get<Me>("/v1/me");
}

/**
 * The graphos endpoints of one tenant, one typed method each.
 * Holds no state beyond the tenant; caching belongs to the data hooks.
 */
export class GraphosApi {
  constructor(
    private readonly http: HttpClient,
    readonly tenant: string,
  ) {}

  private get graph(): string {
    return `/v1/${enc(this.tenant)}/graph`;
  }

  /** Labels, hierarchy and search indexes of the tenant. */
  schema(): Promise<TenantSchema> {
    return this.http.get(`/v1/${enc(this.tenant)}/schema`);
  }

  /** The first `limit` nodes of a label with all their properties. */
  sampleNodes(label: string, limit: number): Promise<NodesScanResponse> {
    return this.scanNodes({ labels: [label], project: ["*"], limit });
  }

  /** One page of nodes in id order; send `next_cursor` back for the next page. */
  async scanNodes(request: NodesScanRequest, signal?: AbortSignal): Promise<NodesScanResponse> {
    return (await this.http.request<NodesScanResponse>("POST", `${this.graph}/nodes:scan`, { body: request, signal }))
      .data;
  }

  /** Ranked search through a search index, or substring on `name` without one. */
  search({ q, index, label, limit = 20, fuzzy }: SearchParams, signal?: AbortSignal): Promise<SearchResponse> {
    return this.http.get(`${this.graph}/search`, { q, index, label, limit, fuzzy }, signal);
  }

  /** One node by its id, with the `ETag` to send back on a write. */
  async node(id: string, label?: string): Promise<NodeRecord> {
    return this.http.get(`${this.graph}/nodes/${enc(id)}`, { label });
  }

  /**
   * Applies a JSON Merge Patch (`null` removes a key). With `etag`, the write
   * happens only if nobody changed the node since it was read (else `412`).
   */
  async patchNode(
    id: string,
    patch: Properties,
    options: { label?: string; etag?: string },
  ): Promise<Tagged<NodeRecord>> {
    return this.http.request("PATCH", `${this.graph}/nodes/${enc(id)}`, {
      body: patch,
      contentType: "application/merge-patch+json",
      query: { label: options.label },
      headers: options.etag ? { "If-Match": options.etag } : undefined,
    });
  }

  /** Deletes a node and every edge touching it. */
  deleteNode(id: string, options: { label?: string; etag?: string }): Promise<Tagged<DeleteNodeResponse>> {
    return this.http.request("DELETE", `${this.graph}/nodes/${enc(id)}`, {
      query: { label: options.label },
      headers: options.etag ? { "If-Match": options.etag } : undefined,
    });
  }

  /** Creates (or updates, merging properties) one node. */
  upsertNode(node: NodeUpsert): Promise<unknown> {
    return this.http.post(`${this.graph}/nodes`, { nodes: [node] });
  }

  /** Creates (or updates) one edge; both end nodes must exist. */
  upsertEdge(edge: EdgeUpsert): Promise<EdgesUpsertResponse> {
    return this.http.post(`${this.graph}/edges`, { edges: [edge] });
  }

  /** One edge by its type and end nodes, with the `ETag` to send back on a write. */
  edge(edge: EdgeKey): Promise<EdgeRecord> {
    return this.http.get(`${this.graph}/edges/${enc(edge.type)}`, edgeQuery(edge));
  }

  /** Applies a JSON Merge Patch to one edge, with `If-Match` like `patchNode`. */
  patchEdge(edge: EdgeKey, patch: Properties, etag?: string): Promise<Tagged<EdgeRecord>> {
    return this.http.request("PATCH", `${this.graph}/edges/${enc(edge.type)}`, {
      body: patch,
      contentType: "application/merge-patch+json",
      query: edgeQuery(edge),
      headers: etag ? { "If-Match": etag } : undefined,
    });
  }

  /** Deletes one edge identified by its type and end nodes. */
  deleteEdge(edge: EdgeKey, etag?: string): Promise<unknown> {
    return this.http.request("DELETE", `${this.graph}/edges/${enc(edge.type)}`, {
      query: edgeQuery(edge),
      headers: etag ? { "If-Match": etag } : undefined,
    });
  }

  /** Edges in and out of a node, with the nodes at the other end. */
  neighbors(id: string, options: { relTypes?: string[]; limit?: number } = {}): Promise<NeighborsResponse> {
    return this.http.get(`${this.graph}/neighbors`, {
      node_id: id,
      rel_types: options.relTypes,
      limit: options.limit,
    });
  }

  /** Every shortest path between two nodes. */
  paths(request: PathsRequest): Promise<PathsResponse> {
    return this.http.post(`${this.graph}/paths`, request);
  }

  /** Up to `limitNodes` nodes and every edge among them. */
  exportGraph(limitNodes: number): Promise<GraphExport> {
    return this.http.get(`${this.graph}/export`, { limit_nodes: limitNodes });
  }

  /** Node and edge counts and sizes per label. */
  stats(): Promise<TenantStats> {
    return this.http.get(`${this.graph}/stats`);
  }

  /** How often each value of one property occurs among a label's nodes. */
  histogram(label: string, property: string, limit = 20): Promise<HistogramResponse> {
    return this.http.get(`${this.graph}/labels/${enc(label)}/histogram`, { property, limit });
  }

  /** The change feed's current position: following from here sees every new change. */
  async changesHead(): Promise<string> {
    return (await this.http.get<{ cursor: string }>(`/v1/${enc(this.tenant)}/changes/head`)).cursor;
  }

  /** Opens the live change stream (Server-Sent Events) after `cursor`. */
  openChangeStream(cursor: string, signal: AbortSignal): Promise<Response> {
    return this.http.open(`/v1/${enc(this.tenant)}/changes/stream`, {
      headers: { Accept: "text/event-stream", "Last-Event-ID": cursor },
      signal,
    });
  }
}

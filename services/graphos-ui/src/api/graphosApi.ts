import type { HttpClient, Tagged } from "./http";
import type {
  DeleteNodeResponse,
  EdgeRecord,
  EdgeUpsert,
  GraphExport,
  GraphMap,
  CommunityMap,
  Job,
  SchemaAddition,
  HubsResponse,
  InducedEdgesResponse,
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
  /** Without `index`: the property to match by substring (default `name`). */
  property?: string;
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

  /** Ranked search through a search index, or substring on `property` (default `name`) without one. */
  search(
    { q, index, label, limit = 20, fuzzy, property }: SearchParams,
    signal?: AbortSignal,
  ): Promise<SearchResponse> {
    return this.http.get(`${this.graph}/search`, { q, index, label, limit, fuzzy, property }, signal);
  }

  /** Every property name some node carries, with the labels that carry it. */
  catalogue(): Promise<{ by_property: Record<string, string[]> }> {
    return this.http.get(`${this.graph}/catalogue`);
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

  /** Every label with its node count and the edge counts between labels; `exact` counts every edge. */
  graphMap(exact = false): Promise<GraphMap> {
    return this.http.get(`${this.graph}/map`, { exact: exact || undefined });
  }

  /** The groups (communities) of the last recompute, largest first, and the edges between them. */
  communityMap(limit = 150): Promise<CommunityMap> {
    return this.http.get(`${this.graph}/communities/map`, { limit });
  }

  /** Starts recomputing the groups as a background job (admin); follow it with `job`. */
  async recomputeGroups(): Promise<Job> {
    return (
      await this.http.request<Job>("POST", `/v1/${enc(this.tenant)}/recompute-communities`, {
        body: {},
        headers: { Prefer: "respond-async" },
      })
    ).data;
  }

  /** The tenant's recent background jobs, newest first. */
  async jobs(limit = 20): Promise<Job[]> {
    return (await this.http.get<{ jobs: Job[] }>("/v1/jobs", { tenant: this.tenant, limit })).jobs;
  }

  /** One background job's status. */
  job(id: string): Promise<Job> {
    return this.http.get(`/v1/jobs/${enc(id)}`);
  }

  /**
   * Adds labels, relationship types, parents or search indexes to the
   * tenant (admin). graphos merges a registration with what exists, so
   * nothing already there is removed.
   */
  addToSchema(addition: SchemaAddition): Promise<unknown> {
    return this.http.post("/v1/tenants", { name: this.tenant, ...addition });
  }

  /** Removes one declared label pair of an edge type (admin); no data is touched. */
  async removeEdgeEndpoint(type: string, src: string, dst: string): Promise<unknown> {
    return (
      await this.http.request("DELETE", `/v1/tenants/${enc(this.tenant)}/edge_endpoints`, {
        query: { type, src, dst },
      })
    ).data;
  }

  /** A label's best-connected nodes, most connected first. */
  hubs(label: string, limit: number): Promise<HubsResponse> {
    return this.http.get(`${this.graph}/map/hubs`, { label, limit });
  }

  /** The edges whose both ends are among `nodeIds`. */
  inducedEdges(nodeIds: string[], limit = 2000): Promise<InducedEdgesResponse> {
    return this.http.post(`${this.graph}/induced_edges`, { node_ids: nodeIds, limit });
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

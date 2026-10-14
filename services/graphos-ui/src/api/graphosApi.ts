import type { HttpClient, Tagged } from "./http";
import type {
  AuthConfig,
  DeleteNodeResponse,
  EdgeRecord,
  EdgeUpsert,
  GraphExport,
  GraphMap,
  CommunityMap,
  ChangesHistory,
  Grant,
  GrantLevel,
  Job,
  JobStatus,
  UserCreated,
  UserInfo,
  DatasetImportResult,
  SchemaAddition,
  SchemaDocument,
  SchemaDryRun,
  HubsResponse,
  InducedEdgesResponse,
  EdgesUpsertResponse,
  HistogramResponse,
  Me,
  Preferences,
  CypherResult,
  SavedQuery,
  NeighborsResponse,
  NodeRecord,
  NodesScanRequest,
  NodesScanResponse,
  NodeUpsert,
  PathsRequest,
  PathsResponse,
  Properties,
  SearchResponse,
  ServiceInfo,
  TenantSchema,
  TenantStats,
  UnregisteredTenant,
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
 * What the signed-in user does outside any one tenant: their preferences, and
 * creating, restoring and deleting tenants.
 */
export class AccountApi {
  constructor(private readonly http: HttpClient) {}

  /** Changes some preferences (`null` removes one); answers all of them. */
  updatePreferences(patch: { [K in keyof Preferences]?: Preferences[K] | null }): Promise<Preferences> {
    return this.http.request<Preferences>("PATCH", "/v1/me/preferences", { body: patch }).then((r) => r.data);
  }

  /**
   * What creating `name` would do, writing nothing: `exists` when it is
   * registered, `restores` when it is unregistered with its data (409 when
   * that data is not the caller's).
   */
  previewTenant(name: string, schema?: SchemaDocument): Promise<SchemaDryRun> {
    return schema
      ? new GraphosApi(this.http, name).previewSchema(schema, { name })
      : this.http.post<SchemaDryRun>("/v1/tenants", { name }, { dry_run: true });
  }

  /** Creates tenant `name`, empty or with the schema of a schema file. */
  createTenant(name: string, schema?: SchemaDocument): Promise<unknown> {
    return schema
      ? new GraphosApi(this.http, name).importSchema(schema, { name })
      : this.http.post("/v1/tenants", { name });
  }

  /** Tenants unregistered with their data kept that the user owns (all, for an admin). */
  async unregisteredTenants(): Promise<UnregisteredTenant[]> {
    const list = await this.http.get<{ unregistered?: UnregisteredTenant[] }>("/v1/tenants", { unregistered: true });
    return list.unregistered ?? [];
  }

  /** Unregisters a tenant, keeping its data; with `dropData`, deletes it for good. */
  async deleteTenant(name: string, dropData: boolean): Promise<void> {
    await this.http.request("DELETE", `/v1/tenants/${enc(name)}`, { query: dropData ? { drop_data: true } : {} });
  }

  /** Registers an unregistered tenant again, as it was. */
  async restoreTenant(name: string): Promise<void> {
    await this.http.post(`/v1/tenants/${enc(name)}/restore`, {});
  }
}

/**
 * The graphos endpoints of one tenant, one typed method each.
 * Holds no state beyond the tenant; caching belongs to the data hooks.
 */
/** Declared label pairs one registration may carry (graphos's limit). */
const MAX_PAIRS_PER_CALL = 5000;

export interface ImportOptions {
  /** Another tenant to import into (created when new); this one by default. */
  name?: string;
  /** Replace the registered schema instead of adding to it. */
  replace?: boolean;
}

function byType(pairs: (readonly [string, unknown])[]): Record<string, unknown[]> {
  const out: Record<string, unknown[]> = {};
  for (const [type, pair] of pairs) (out[type] ??= []).push(pair);
  return out;
}

/** Said in the preview when it could not include every pair (the import does). */
export function morePairsWarning(count: number): string {
  return `${count} more declared label pairs are not in this preview (at most 5000 per call); the import adds them`;
}

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

  /**
   * Starts computing graph metrics as a background job (`maintain`); follow it
   * with `jobs`. Over the relationships of `relTypes` only, or all of them.
   */
  async computeMetrics(metrics: string[], relTypes: string[] = []): Promise<Job> {
    return (
      await this.http.request<Job>("POST", `${this.graph}/metrics:compute`, {
        body: relTypes.length > 0 ? { metrics, rel_types: relTypes } : { metrics },
        headers: { Prefer: "respond-async" },
      })
    ).data;
  }

  /** Starts recomputing the groups as a background job (`maintain`); follow it with `jobs`. */
  async recomputeGroups(): Promise<Job> {
    return (
      await this.http.request<Job>("POST", `/v1/${enc(this.tenant)}/recompute-communities`, {
        body: {},
        headers: { Prefer: "respond-async" },
      })
    ).data;
  }

  /** The tenant's recent jobs, newest first; `status` keeps only those states. */
  async jobs(limit = 20, status?: JobStatus[]): Promise<Job[]> {
    return (await this.http.get<{ jobs: Job[] }>("/v1/jobs", { tenant: this.tenant, limit, status })).jobs;
  }

  /** Users and service accounts (admin). */
  async users(): Promise<UserInfo[]> {
    return (await this.http.get<{ users: UserInfo[] }>("/v1/users")).users;
  }

  /** Creates a person or a service account (admin); the password comes back once. */
  createUser(name: string, kind: "person" | "service"): Promise<UserCreated> {
    return this.http.post("/v1/users", { name, kind });
  }

  /** Disables or enables a user (admin); a disabled user's tokens stop at once. */
  async setUserDisabled(name: string, disabled: boolean): Promise<UserInfo> {
    return (await this.http.request<UserInfo>("PATCH", `/v1/users/${enc(name)}`, { body: { disabled } })).data;
  }

  /** Deletes a user and its grants (admin). */
  async deleteUser(name: string): Promise<void> {
    await this.http.request("DELETE", `/v1/users/${enc(name)}`);
  }

  /** A new password or secret for a user (admin), shown once. */
  resetUserSecret(name: string): Promise<UserCreated> {
    return this.http.post(`/v1/users/${enc(name)}:reset-secret`, undefined);
  }

  /** The service and the optional parts this server has, such as Louvain. */
  info(): Promise<ServiceInfo> {
    return this.http.get<ServiceInfo>("/v1/info");
  }

  /** How people sign in here: passwords, and the identity provider when single sign-on is set up. */
  authConfig(): Promise<AuthConfig> {
    return this.http.get<AuthConfig>("/v1/auth/config");
  }

  /** Grants: all for an admin; for an owner, its own and those on the tenants it owns. */
  async grants(filter: { subject?: string; tenant?: string } = {}): Promise<Grant[]> {
    return (await this.http.get<{ grants: Grant[] }>("/v1/grants", filter)).grants;
  }

  /** Gives a level on a tenant or pattern, or a global right. */
  grant(subject: string, tenant: string, level: GrantLevel): Promise<Grant> {
    return this.http.post("/v1/grants", { subject, tenant, level });
  }

  /** Takes a grant back. */
  async revoke(grant: Grant): Promise<void> {
    await this.http.request("DELETE", "/v1/grants", {
      query: { subject: grant.subject, tenant: grant.tenant, level: grant.level },
    });
  }

  /** Cancels a queued or running job, or a synchronous call listed as one. */
  cancelJob(id: string): Promise<Job> {
    return this.http.post(`/v1/jobs/${enc(id)}:cancel`, undefined);
  }

  /** Earlier changes, newest first: before `before` (or the newest), only `ops` when given. */
  changesHistory(before?: string, ops?: string[], limit = 50): Promise<ChangesHistory> {
    return this.http.get(`/v1/${enc(this.tenant)}/changes/history`, { before, op: ops, limit });
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

  /**
   * The tenant's data as one file, as a response to stream: a header line,
   * every node, every edge, an end line (`read`; recorded in the audit log).
   */
  exportDataset(options: { gzip?: boolean; computed?: boolean; signal?: AbortSignal } = {}): Promise<Response> {
    return this.http.open(`/v1/${enc(this.tenant)}/graph/snapshot`, {
      query: { gzip: options.gzip ?? false, computed: options.computed ?? true },
      headers: { Accept: "*/*" },
      signal: options.signal,
    });
  }

  /**
   * Imports a dataset file (gzip or not), sent as it is. `empty` imports only
   * into a tenant without nodes (409 otherwise); `merge` adds to what is there.
   */
  importDataset(
    file: Blob,
    mode: "merge" | "empty" = "merge",
    progress: { onSent?: (bytes: number, total: number) => void; signal?: AbortSignal } = {},
  ): Promise<DatasetImportResult> {
    return this.http.upload<DatasetImportResult>(`/v1/${enc(this.tenant)}/graph/snapshot-import`, file, {
      contentType: "application/x-ndjson",
      query: { mode },
      ...progress,
    });
  }

  /** Runs a read-only Cypher query on the tenant's graph (the console, when on). */
  cypher(
    query: string,
    params: Record<string, unknown>,
    options: { limit?: number; signal?: AbortSignal } = {},
  ): Promise<CypherResult> {
    return this.http
      .request<CypherResult>("POST", `/v1/${enc(this.tenant)}/cypher`, {
        body: { query, params, ...(options.limit ? { limit: options.limit } : {}) },
        signal: options.signal,
      })
      .then((r) => r.data);
  }

  savedQueries(): Promise<SavedQuery[]> {
    return this.http.get(`/v1/${enc(this.tenant)}/cypher/saved`);
  }

  /** Saves a query under `name`, replacing one of that name (`maintain`). */
  saveQuery(name: string, query: Omit<SavedQuery, "name" | "created_by" | "updated_at">): Promise<SavedQuery> {
    return this.http
      .request<SavedQuery>("PUT", `/v1/${enc(this.tenant)}/cypher/saved/${enc(name)}`, { body: query })
      .then((r) => r.data);
  }

  async deleteSavedQuery(name: string): Promise<void> {
    await this.http.request("DELETE", `/v1/${enc(this.tenant)}/cypher/saved/${enc(name)}`);
  }

  runSavedQuery(name: string, params: Record<string, unknown>, signal?: AbortSignal): Promise<CypherResult> {
    return this.http
      .request<CypherResult>("POST", `/v1/${enc(this.tenant)}/cypher/saved/${enc(name)}:run`, {
        body: { params },
        signal,
      })
      .then((r) => r.data);
  }

  /** The tenant's whole schema as a file (`maintain`). */
  exportSchema(): Promise<SchemaDocument> {
    return this.http.get(`/v1/tenants/${enc(this.tenant)}/spec`);
  }

  /** What importing `doc` would change, writing nothing (see {@link importSchema}). */
  async previewSchema(doc: SchemaDocument, options: ImportOptions = {}): Promise<SchemaDryRun> {
    return (await this.importSchemaCalls(doc, options, true)) as SchemaDryRun;
  }

  /**
   * Registers the schema in `doc`: into this tenant, or `options.name`
   * (created when new); added to the registered schema, or replacing it with
   * `options.replace`. A registration carries at most 5,000 declared label
   * pairs, so a document with more is sent in several calls: the first with
   * everything else, the rest adding pairs.
   */
  async importSchema(doc: SchemaDocument, options: ImportOptions = {}): Promise<unknown> {
    return this.importSchemaCalls(doc, options, false);
  }

  private async importSchemaCalls(doc: SchemaDocument, options: ImportOptions, dryRun: boolean) {
    const name = options.name ?? this.tenant;
    const pairs = Object.entries(doc.edge_endpoints ?? {}).flatMap(([type, list]) =>
      list.map((pair) => [type, pair] as const),
    );
    const chunks: (readonly [string, unknown])[][] = [];
    for (let i = 0; i < pairs.length; i += MAX_PAIRS_PER_CALL) chunks.push(pairs.slice(i, i + MAX_PAIRS_PER_CALL));
    const query = { ...(options.replace ? { force_shrink: true } : {}), ...(dryRun ? { dry_run: true } : {}) };
    const first = { ...doc, name, ...(doc.edge_endpoints ? { edge_endpoints: byType(chunks[0] ?? []) } : {}) };
    let result = await this.http.post<unknown>("/v1/tenants", first, query);
    if (dryRun) {
      const rest = pairs.length - (chunks[0]?.length ?? 0);
      const preview = result as SchemaDryRun;
      return rest > 0 ? { ...preview, warnings: [...preview.warnings, morePairsWarning(rest)] } : preview;
    }
    for (const chunk of chunks.slice(1)) {
      result = await this.http.post<unknown>("/v1/tenants", { name, edge_endpoints: byType(chunk) });
    }
    return result;
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

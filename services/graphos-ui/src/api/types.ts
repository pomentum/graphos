/**
 * Shapes of the graphos REST API that the UI uses, as the server returns them.
 * Field names follow the API (snake_case); see the OpenAPI schema at /docs.
 */

export type Properties = Record<string, unknown>;
export type Direction = "out" | "in" | "both";

/** `GET /v1/me`: the signed-in token. */
export type Level = "read" | "write" | "maintain" | "owner";

/** Who is signed in (`GET /v1/me`): their level on each tenant and their rights. */
export interface Me {
  consumer: string;
  kind: "person" | "service" | "anonymous";
  /** How they signed in: a graphos password, or the identity provider. */
  via: "password" | "oidc" | "none";
  admin: boolean;
  /** The password must be changed before anything else. */
  must_change: boolean;
  tenants: string[];
  levels: Record<string, Level>;
  /** Patterns of tenant names this user may create. */
  create_tenants: string[];
}

/** Answer of `/v1/auth/login`, `/v1/auth/refresh` and `/v1/me/password`. */
export interface TokenResponse {
  access_token: string;
  expires_in: number;
  /** People only: renews the access token, rotated on each use. Null for the UI: its
   *  session asks graphos to keep it in an HttpOnly cookie the page cannot read. */
  refresh_token: string | null;
  /** A session can renew this token: `refresh_token`, or the session cookie. */
  session?: boolean;
  must_change: boolean;
}

/** Answer of `/v1/auth/oidc/callback`: the tokens and the page to return to. */
export interface OidcSignedIn extends TokenResponse {
  return_to: string;
}

/** How to sign in (`GET /v1/auth/config`). */
export interface AuthConfig {
  password: boolean;
  oidc: { issuer: string; client_id: string; scopes: string; display_name: string } | null;
}

export type GrantLevel = Level | "create_tenants" | "admin";

/** `GET /v1/info`: the service, and the optional parts this server has (no sign-in needed). */
export interface ServiceInfo {
  service: string;
  version: string;
  features: { louvain: boolean; metrics: boolean };
}

/** A user or service account (`/v1/users`, admin). */
export interface UserInfo {
  name: string;
  kind: "person" | "service";
  disabled: boolean;
  must_change: boolean;
  created_at: string;
  created_by: string | null;
  last_login_at: string | null;
}

/** A user just created or reset, with its password shown once. */
export interface UserCreated {
  user: UserInfo;
  password: string;
}

/** What a subject may do on a tenant (or pattern), or a global right. */
export interface Grant {
  subject: string;
  tenant: string;
  level: GrantLevel;
  granted_by?: string | null;
  granted_at?: string;
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
  /** Per edge type, the label pairs it is declared to join (absent from older servers). */
  edge_endpoints?: Record<string, { src: string; dst: string }[]>;
  /** Writes of a declared type outside its pairs are refused. */
  strict_endpoints?: boolean;
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
/** One condition of a node scan; conditions are combined with AND. */
export interface ScanFilter {
  property: string;
  op: "=" | "!=" | "exists" | "not exists";
  /** Omitted for `exists` / `not exists`. */
  value?: unknown;
}

/** A page of a label's nodes, in id order. */
export interface NodesScanRequest {
  labels: string[];
  where?: ScanFilter[];
  /** Properties to return; `["*"]` for all of them. */
  project?: string[];
  limit?: number;
  cursor?: string | null;
  /** Sort by these properties (nodes without the first are left out); `null` for no order. */
  order_by?: { property: string; desc?: boolean }[] | null;
}

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

/** One edge with its revision; `etag` goes back as `If-Match` on a write. */
export interface EdgeRecord {
  type: string;
  src_id: string;
  src_label: string;
  dst_id: string;
  dst_label: string;
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

/** `GET …/graph/map`: every label with its node count, and the edge counts between labels. */
export interface GraphMap {
  labels: { label: string; count: number }[];
  links: { src_label: string; type: string; dst_label: string; count: number; estimated: boolean }[];
  /** Some link counts come from a sample of a large edge table. */
  estimated: boolean;
}

/** `GET …/graph/map/hubs`: a label's best-connected nodes. */
export interface HubsResponse {
  label: string;
  nodes: { id: string; label: string; properties: Properties; degree: number; estimated: boolean }[];
}

/** `POST …/graph/induced_edges`: the edges among a set of node ids. */
export interface InducedEdgesResponse {
  edges: { src_id: string; dst_id: string; type: string; properties: Properties }[];
  truncated: boolean;
}

/** `GET …/graph/communities/map`: the groups of the last recompute and the edges between them. */
export interface CommunityMap {
  /** False until a recompute has stored a map. */
  available: boolean;
  computed_at: string | null;
  modularity: number | null;
  /** All communities; `communities` holds the largest of them. */
  n_communities: number;
  communities: {
    community_id: number;
    size: number;
    /** Members per label, most first. */
    labels: Record<string, number>;
    hub: { id: string; label: string; properties: Properties; degree: number } | null;
    internal_edges: number;
  }[];
  links: { src: number; dst: number; count: number; types: { type: string; count: number }[] }[];
}

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

/**
 * A background job (`/v1/jobs`), or a synchronous call of a job kind
 * (`sync`: its caller waits for the answer), listed while it runs.
 */
export interface Job {
  id: string;
  kind: string;
  tenant: string | null;
  status: JobStatus;
  params: Record<string, unknown>;
  result: Record<string, unknown> | null;
  /** The HTTP status and detail the call answered with. */
  error: { status: number; detail: string } | null;
  consumer: string | null;
  cancel_requested: boolean;
  sync: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

/** What an admin may add to a tenant's schema: re-registration merges it with what exists. */
export interface SchemaAddition {
  vertex_labels?: string[];
  edge_labels?: string[];
  /** child label -> parent label */
  label_hierarchy?: Record<string, string>;
  search_indexes?: Record<string, SearchIndexSpec>;
  /** Label pairs to declare, per edge type: added to the declared ones. */
  edge_endpoints?: Record<string, [string, string][]>;
  strict_endpoints?: boolean;
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

/** What a schema change added, removed or switched (`op: "schema"`). */
export interface SchemaChangeDetail {
  added?: Record<string, unknown[]>;
  removed?: Record<string, unknown[]>;
  /** [before, after] */
  strict_endpoints?: [boolean, boolean];
  registered?: boolean;
  unregistered?: boolean;
}

/** One record of the change feed: what changed and who changed it, not the new values. */
export interface ChangeItem {
  cursor: string;
  at: string;
  op: "upsert" | "merge" | "patch" | "delete" | "resolve" | "communities" | "metrics" | "schema";
  kind: "node" | "edge" | "tenant";
  label: string | null;
  src_label: string | null;
  dst_label: string | null;
  ids: string[] | null;
  edges: [string, string][] | null;
  rev: number | null;
  count: number;
  /** Per-op detail; for `schema`, a `SchemaChangeDetail`. */
  detail?: Record<string, unknown> | null;
  consumer: string | null;
}

/** Earlier changes, newest first (`/changes/history`). */
export interface ChangesHistory {
  changes: ChangeItem[];
  /** Pass as `before` for the page before; null at the end. */
  older: string | null;
  more: boolean;
}

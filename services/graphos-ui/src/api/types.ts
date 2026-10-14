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
  /** What the user chose in the UI, kept by graphos (not in the browser). */
  preferences?: Preferences;
}

/** Preferences graphos keeps per user (`PATCH /v1/me/preferences`; `null` removes one). */
export interface Preferences {
  theme?: "auto" | "light" | "dark";
  last_tenant?: string;
}

/** A tenant unregistered with its data kept (`GET /v1/tenants?unregistered=true`). */
export interface UnregisteredTenant {
  name: string;
  /** `orphan`: unregistered before 0.8.2, no kept registration (admins only). */
  kind: "unregistered" | "orphan";
  vertex_labels: number;
  edge_labels: number;
  created_at: string | null;
  unregistered_at: string | null;
  unregistered_by: string | null;
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
  features: { louvain: boolean; metrics: boolean; cypher?: boolean };
}

/** A node in a Cypher result: its graphos id, its own label, its properties. */
export interface CypherNode {
  id: string;
  label: string;
  properties: Properties;
}

/** An edge in a Cypher result; its ends are null when they were not returned. */
export interface CypherEdge {
  type: string;
  properties: Properties;
  src_id: string | null;
  src_label: string | null;
  dst_id: string | null;
  dst_label: string | null;
}

/** A cell: a node, an edge, a path (a list of them), or a plain value. */
export type CypherCell = { node: CypherNode } | { edge: CypherEdge } | CypherCell[] | unknown;

/** `POST /v1/{tenant}/cypher`: a table, and the graph its nodes and edges make. */
export interface CypherResult {
  columns: string[];
  rows: CypherCell[][];
  graph: { nodes: CypherNode[]; edges: (CypherEdge & { src_id: string; dst_id: string })[] };
  truncated: boolean;
  elapsed_ms: number;
}

export interface SavedQueryParam {
  type: "string" | "number" | "boolean" | "list";
  label?: string | null;
}

/** A saved Cypher query (`/v1/{tenant}/cypher/saved`). */
export interface SavedQuery {
  name: string;
  description: string;
  query: string;
  params: Record<string, SavedQueryParam>;
  show: "table" | "graph";
  created_by?: string | null;
  updated_at?: string | null;
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
  /** How labels and edge types are drawn, as the tenant declared (absent from older servers). */
  display?: DisplaySpec;
}

/** The pictograms a label may be drawn with (graphos's fixed list). */
export const DISPLAY_ICONS = [
  "person",
  "people",
  "building",
  "document",
  "folder",
  "code",
  "function",
  "database",
  "server",
  "cloud",
  "package",
  "tag",
  "link",
  "calendar",
  "location",
  "mail",
  "key",
  "lock",
  "shield",
  "bug",
  "star",
  "flag",
  "chart",
  "settings",
  "globe",
  "book",
  "cpu",
  "branch",
  "message",
  "box",
] as const;
export type DisplayIcon = (typeof DISPLAY_ICONS)[number];

/** How one vertex label is drawn; every key optional (the server answers left-out keys as null). */
export interface VertexDisplay {
  color?: string | null;
  caption?: string | null;
  size_by?: string | null;
  icon?: DisplayIcon | null;
}

export interface EdgeDisplay {
  color?: string | null;
  caption?: string | null;
}

/** `display`: per label and edge type; in a registration, `null` removes an entry. */
export interface DisplaySpec {
  vertex?: Record<string, VertexDisplay | null>;
  edge?: Record<string, EdgeDisplay | null>;
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
/**
 * `GET /v1/tenants/{name}/spec`: the tenant's whole schema as a file. It is a
 * registration body (posting it registers that schema) with an `_export`
 * header saying what it is.
 */
export interface SchemaDocument {
  _export?: { format?: string; version?: number; graphos?: string; tenant?: string; exported_at?: string };
  name: string;
  vertex_labels?: string[];
  /** Per edge type, its declared label pairs (`{src, dst}` or `[src, dst]`). */
  edge_endpoints?: Record<string, unknown[]>;
  [part: string]: unknown;
}

/** The first line of a dataset file (`GET …/graph/snapshot`): what the file is. */
export interface DatasetHeader {
  _kind: "header";
  format: string;
  version: number;
  graphos?: string;
  tenant?: string;
  exported_at?: string;
  computed?: boolean;
  counts?: { nodes: number; edges: number };
  schema?: SchemaDocument;
}

/** `POST …/graph/snapshot-import`: what a dataset import wrote. */
export interface DatasetImportResult {
  tenant: string;
  mode: "merge" | "empty";
  nodes_received: number;
  edges_received: number;
  nodes_upserted: number;
  edges_upserted: number;
  edges_skipped: number;
  /** The file's header without its schema; null for a file without one. */
  header: Omit<DatasetHeader, "schema"> | null;
  /** False when the file ended before its end line (it was cut off). */
  complete: boolean | null;
  errors: string[];
  elapsed_ms: number;
}

/** `POST /v1/tenants?dry_run=true`: what importing a schema would change. */
export interface SchemaDryRun {
  tenant: string;
  exists: boolean;
  /** The tenant is unregistered with its data kept: registering it restores it first. */
  restores?: boolean;
  mode: "merge" | "replace";
  /** What the change feed would record; null when nothing would change. */
  changes: SchemaChangeDetail | null;
  /** What applying it would cost (a table rewrite, a long build) or could still fail on. */
  warnings: string[];
}

export interface SchemaAddition {
  vertex_labels?: string[];
  edge_labels?: string[];
  /** child label -> parent label */
  label_hierarchy?: Record<string, string>;
  search_indexes?: Record<string, SearchIndexSpec>;
  /** Label pairs to declare, per edge type: added to the declared ones. */
  edge_endpoints?: Record<string, [string, string][]>;
  strict_endpoints?: boolean;
  /** Styles to declare (an entry replaces the stored one; `null` removes it). */
  display?: DisplaySpec;
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
  /** Parts changed in place: `meta` keys whose text changed. */
  changed?: Record<string, unknown[]>;
  /** [before, after] */
  strict_endpoints?: [boolean, boolean];
  registered?: boolean;
  unregistered?: boolean;
  /** Registered again after being unregistered, as it was. */
  restored?: boolean;
  /** Deleted with its data. */
  data_deleted?: boolean;
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

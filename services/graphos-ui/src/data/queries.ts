import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { EdgeKey, SearchParams } from "../api/graphosApi";
import type { EdgeUpsert, NodeUpsert, PathsRequest, Properties } from "../api/types";
import { useTenantSession } from "../session/SessionProvider";

/** Cache keys, scoped by tenant so switching tenants never shows another's data. */
export const keys = {
  schema: (tenant: string) => ["schema", tenant] as const,
  search: (tenant: string, params: SearchParams) => ["search", tenant, params] as const,
  node: (tenant: string, label: string, id: string) => ["node", tenant, label, id] as const,
  edge: (tenant: string, edge: EdgeKey) =>
    ["edge", tenant, edge.type, edge.srcLabel, edge.srcId, edge.dstLabel, edge.dstId] as const,
  stats: (tenant: string) => ["stats", tenant] as const,
  properties: (tenant: string, label: string) => ["properties", tenant, label] as const,
  histogram: (tenant: string, label: string, property: string) => ["histogram", tenant, label, property] as const,
};

/** Labels, hierarchy and search indexes; changes rarely, so cached for minutes. */
export function useSchema() {
  const { api, tenant } = useTenantSession();
  return useQuery({ queryKey: keys.schema(tenant), queryFn: () => api.schema(), staleTime: 5 * 60_000 });
}

/** Search results for `params`; runs only for a non-empty query and keeps the last results while typing. */
export function useSearch(params: SearchParams) {
  const { api, tenant } = useTenantSession();
  return useQuery({
    queryKey: keys.search(tenant, params),
    queryFn: ({ signal }) => api.search(params, signal),
    enabled: params.q.trim().length > 0,
    placeholderData: keepPreviousData,
    staleTime: 10_000,
  });
}

/** One node with its current revision and `ETag`. */
export function useNode(ref: { id: string; label: string } | null) {
  const { api, tenant } = useTenantSession();
  return useQuery({
    queryKey: ref ? keys.node(tenant, ref.label, ref.id) : ["node", tenant, "none"],
    queryFn: () => api.node(ref!.id, ref!.label),
    enabled: ref !== null,
  });
}

/** Nodes read to learn which properties a label uses; cheap and always current. */
const PROPERTY_SAMPLE = 200;

/** Property names used by a label's nodes, from a sample of them, sorted. */
export function useProperties(label: string | null) {
  const { api, tenant } = useTenantSession();
  return useQuery({
    queryKey: keys.properties(tenant, label ?? ""),
    queryFn: () => api.sampleNodes(label!, PROPERTY_SAMPLE),
    enabled: label !== null,
    staleTime: 60_000,
    select: (page) => [...new Set(page.items.flatMap((node) => Object.keys(node.properties)))].sort(),
  });
}

/** One edge with its current revision and `ETag`. */
export function useEdge(edge: EdgeKey | null) {
  const { api, tenant } = useTenantSession();
  return useQuery({
    queryKey: edge ? keys.edge(tenant, edge) : ["edge", tenant, "none"],
    queryFn: () => api.edge(edge!),
    enabled: edge !== null,
  });
}

/** Saves edited edge properties with `If-Match`. */
export function useSaveEdge() {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { edge: EdgeKey; patch: Properties; etag: string }) =>
      api.patchEdge(args.edge, args.patch, args.etag),
    onSuccess: (result, args) => client.setQueryData(keys.edge(tenant, args.edge), result.data),
  });
}

/** Counts and sizes per label. */
export function useStats() {
  const { api, tenant } = useTenantSession();
  return useQuery({ queryKey: keys.stats(tenant), queryFn: () => api.stats(), staleTime: 30_000 });
}

/** Value counts of one property of one label; idle until both are chosen. */
export function useHistogram(label: string | null, property: string | null) {
  const { api, tenant } = useTenantSession();
  return useQuery({
    queryKey: keys.histogram(tenant, label ?? "", property ?? ""),
    queryFn: () => api.histogram(label!, property!),
    enabled: Boolean(label && property),
  });
}

/** Neighbours of a node, fetched on demand when the user expands it. */
export function useExpand() {
  const { api } = useTenantSession();
  return useMutation({
    mutationFn: (args: { id: string; relTypes?: string[]; limit: number }) =>
      api.neighbors(args.id, { relTypes: args.relTypes, limit: args.limit }),
  });
}

/** Shortest paths between two nodes. */
export function usePaths() {
  const { api } = useTenantSession();
  return useMutation({ mutationFn: (request: PathsRequest) => api.paths(request) });
}

/** Saves edited properties with `If-Match`; a `412` means someone else saved first. */
export function useSaveNode() {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { id: string; label: string; patch: Properties; etag: string }) =>
      api.patchNode(args.id, args.patch, { label: args.label, etag: args.etag }),
    onSuccess: (result, args) => client.setQueryData(keys.node(tenant, args.label, args.id), result.data),
  });
}

/** Deletes a node (and its edges) if it is still the version shown. */
export function useDeleteNode() {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { id: string; label: string; etag?: string }) =>
      api.deleteNode(args.id, { label: args.label, etag: args.etag }),
    onSuccess: (_, args) => {
      client.removeQueries({ queryKey: keys.node(tenant, args.label, args.id) });
      void client.invalidateQueries({ queryKey: keys.stats(tenant) });
    },
  });
}

/** Creates a node. */
export function useCreateNode() {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (node: NodeUpsert) => api.upsertNode(node),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.stats(tenant) }),
  });
}

/** Creates an edge between two existing nodes. */
export function useCreateEdge() {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (edge: EdgeUpsert) => api.upsertEdge(edge),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.stats(tenant) }),
  });
}

/** Deletes one edge. */
export function useDeleteEdge() {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (edge: EdgeKey & { etag?: string }) => api.deleteEdge(edge, edge.etag),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.stats(tenant) }),
  });
}

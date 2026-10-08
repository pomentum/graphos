import type { ParseKeys } from "i18next";
import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { EdgeKey, SearchParams } from "../api/graphosApi";
import type {
  EdgeUpsert,
  Grant,
  GrantLevel,
  Job,
  NodesScanRequest,
  NodeUpsert,
  PathsRequest,
  Properties,
  SchemaAddition,
} from "../api/types";
import { CATEGORY_OPS, type ActivityCategory } from "../lib/changes";
import { isActiveJob } from "../lib/jobs";
import { profileProperties } from "../lib/properties";
import { useTenantSession } from "../session/SessionProvider";

/**
 * What a mutation says when it ends: `success` and `errorTitle` are i18n keys
 * (translated by the MutationCache in App), `silent` leaves errors to the caller.
 */
declare module "@tanstack/react-query" {
  interface Register {
    mutationMeta: { success?: ParseKeys; errorTitle?: ParseKeys; silent?: boolean };
  }
}

/** Cache keys, scoped by tenant so switching tenants never shows another's data. */
export const keys = {
  schema: (tenant: string) => ["schema", tenant] as const,
  search: (tenant: string, params: SearchParams) => ["search", tenant, params] as const,
  node: (tenant: string, label: string, id: string) => ["node", tenant, label, id] as const,
  edge: (tenant: string, edge: EdgeKey) =>
    ["edge", tenant, edge.type, edge.srcLabel, edge.srcId, edge.dstLabel, edge.dstId] as const,
  stats: (tenant: string) => ["stats", tenant] as const,
  properties: (tenant: string, label: string) => ["properties", tenant, label] as const,
  scan: (tenant: string, request: NodesScanRequest) => ["scan", tenant, request] as const,
  histogram: (tenant: string, label: string, property: string) => ["histogram", tenant, label, property] as const,
  catalogue: (tenant: string) => ["catalogue", tenant] as const,
  map: (tenant: string, exact: boolean) => ["map", tenant, exact] as const,
  groups: (tenant: string) => ["groups", tenant] as const,
  groupsJob: (tenant: string) => ["groupsJob", tenant] as const,
  activeJobs: (tenant: string) => ["activeJobs", tenant] as const,
  recentJobs: (tenant: string) => ["recentJobs", tenant] as const,
  history: (tenant: string, category: ActivityCategory | "all") => ["history", tenant, category] as const,
  users: () => ["users"] as const,
  grants: (tenant: string | null) => ["grants", tenant] as const,
  authConfig: () => ["auth-config"] as const,
};

/** Labels, hierarchy and search indexes; changes rarely, so cached for minutes. */
export function useSchema() {
  const { api, tenant } = useTenantSession();
  return useQuery({ queryKey: keys.schema(tenant), queryFn: () => api.schema(), staleTime: 5 * 60_000 });
}

/** The tenant's labels and the links between them; counted again at most every few minutes. */
export function useGraphMap(exact = false) {
  const { api, tenant } = useTenantSession();
  return useQuery({ queryKey: keys.map(tenant, exact), queryFn: () => api.graphMap(exact), staleTime: 5 * 60_000 });
}

/** The groups of the last recompute and the edges between them. */
export function useCommunityMap() {
  const { api, tenant } = useTenantSession();
  return useQuery({ queryKey: keys.groups(tenant), queryFn: () => api.communityMap(), staleTime: 5 * 60_000 });
}

/** The property catalogue: each property name and the labels whose nodes carry it. */
export function useCatalogue() {
  const { api, tenant } = useTenantSession();
  return useQuery({ queryKey: keys.catalogue(tenant), queryFn: () => api.catalogue(), staleTime: 5 * 60_000 });
}

export { isActiveJob };

/** How often a running job is checked. */
const JOB_POLL_MS = 3_000;

/**
 * The tenant's latest groups recompute, from graphos's job list, checked
 * every few seconds while it runs and not at all otherwise.
 */
export function useGroupsJob() {
  const { api, tenant } = useTenantSession();
  return useQuery({
    queryKey: keys.groupsJob(tenant),
    queryFn: async () => (await api.jobs()).find((job) => job.kind === "recompute_communities") ?? null,
    refetchInterval: (query) => (isActiveJob(query.state.data) ? JOB_POLL_MS : false),
  });
}

/** How often the running jobs are checked: often while some run, otherwise to notice new ones. */
const ACTIVE_POLL_MS = 3_000;
const IDLE_POLL_MS = 15_000;

/**
 * What is running on the tenant now, whoever started it: background jobs and
 * synchronous recomputes, metrics runs, resolutions and vacuums.
 */
export function useActiveJobs() {
  const { api, tenant } = useTenantSession();
  return useQuery({
    queryKey: keys.activeJobs(tenant),
    queryFn: () => api.jobs(50, ["queued", "running"]),
    refetchInterval: (query) => (query.state.data?.length ? ACTIVE_POLL_MS : IDLE_POLL_MS),
  });
}

/** The tenant's latest jobs, finished ones included; read again when the running ones change. */
export function useRecentJobs() {
  const { api, tenant } = useTenantSession();
  return useQuery({ queryKey: keys.recentJobs(tenant), queryFn: () => api.jobs(30), refetchInterval: 60_000 });
}

/** Cancels a running job (or a synchronous call listed as one). */
export function useCancelJob() {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (job: Job) => api.cancelJob(job.id),
    meta: { success: "common.jobs.cancelRequested", errorTitle: "common.jobs.cancelFailed" },
    onSettled: () => client.invalidateQueries({ queryKey: keys.activeJobs(tenant) }),
  });
}

/** Earlier changes, newest first, a page at a time; only one category's ops unless "all". */
export function useChangeHistory(category: ActivityCategory | "all") {
  const { api, tenant } = useTenantSession();
  return useInfiniteQuery({
    queryKey: keys.history(tenant, category),
    queryFn: ({ pageParam }) => api.changesHistory(pageParam, category === "all" ? undefined : CATEGORY_OPS[category]),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.older ?? undefined,
  });
}

/** Users and service accounts (admins only). */
export function useUsers(enabled: boolean) {
  const { api } = useTenantSession();
  return useQuery({ queryKey: keys.users(), queryFn: () => api.users(), enabled });
}

/** How people sign in: whether single sign-on is set up (it changes only with a restart). */
export function useAuthConfig() {
  const { api } = useTenantSession();
  return useQuery({ queryKey: keys.authConfig(), queryFn: () => api.authConfig(), staleTime: Infinity });
}

/** Grants: those that apply to `tenant`, or every grant (admins) when null. */
export function useGrants(tenant: string | null) {
  const { api } = useTenantSession();
  return useQuery({
    queryKey: keys.grants(tenant),
    queryFn: () => api.grants(tenant ? { tenant } : {}),
  });
}

/** What changes users and grants; everything about them is read again after. */
export function useAccessMutations() {
  const { api } = useTenantSession();
  const client = useQueryClient();
  const refresh = () =>
    Promise.all([
      client.invalidateQueries({ queryKey: ["users"] }),
      client.invalidateQueries({ queryKey: ["grants"] }),
    ]);
  return {
    createUser: useMutation({
      meta: { errorTitle: "views.access.errors.notCreated" },
      mutationFn: ({ name, kind }: { name: string; kind: "person" | "service" }) => api.createUser(name, kind),
      onSuccess: refresh,
    }),
    resetSecret: useMutation({
      meta: { errorTitle: "views.access.errors.notReset" },
      mutationFn: (name: string) => api.resetUserSecret(name),
      onSuccess: refresh,
    }),
    setDisabled: useMutation({
      meta: { errorTitle: "views.access.errors.notChanged" },
      mutationFn: ({ name, disabled }: { name: string; disabled: boolean }) => api.setUserDisabled(name, disabled),
      onSuccess: refresh,
    }),
    deleteUser: useMutation({
      meta: { success: "views.access.notices.deleted", errorTitle: "views.access.errors.notChanged" },
      mutationFn: (name: string) => api.deleteUser(name),
      onSuccess: refresh,
    }),
    grant: useMutation({
      meta: { success: "views.access.notices.granted", errorTitle: "views.access.errors.notGranted" },
      mutationFn: ({ subject, tenant, level }: { subject: string; tenant: string; level: GrantLevel }) =>
        api.grant(subject, tenant, level),
      onSuccess: refresh,
    }),
    revoke: useMutation({
      meta: { success: "views.access.notices.revoked", errorTitle: "views.access.errors.notRevoked" },
      mutationFn: (grant: Grant) => api.revoke(grant),
      onSuccess: refresh,
    }),
  };
}

/** Every property name nodes of the tenant carry, sorted; for "search in" pickers. */
export function usePropertyNames() {
  const { api, tenant } = useTenantSession();
  return useQuery({
    queryKey: keys.catalogue(tenant),
    queryFn: () => api.catalogue(),
    staleTime: 5 * 60_000,
    select: (catalogue) => Object.keys(catalogue.by_property).sort(),
  });
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

/** The properties of a label (from the same sample), most common first, with an example value each. */
export function usePropertyProfile(label: string | null) {
  const { api, tenant } = useTenantSession();
  return useQuery({
    queryKey: keys.properties(tenant, label ?? ""),
    queryFn: () => api.sampleNodes(label!, PROPERTY_SAMPLE),
    enabled: label !== null,
    staleTime: 60_000,
    select: (page) => profileProperties(page.items),
  });
}

/** One page of a node scan; idle without a request, and keeps the last page while the next loads. */
export function useNodePage(request: NodesScanRequest | null) {
  const { api, tenant } = useTenantSession();
  return useQuery({
    queryKey: request ? keys.scan(tenant, request) : ["scan", tenant, "none"],
    queryFn: ({ signal }) => api.scanNodes(request!, signal),
    enabled: request !== null,
    placeholderData: keepPreviousData,
    staleTime: 10_000,
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
    meta: { success: "notices.saved" },
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
    meta: { errorTitle: "errors.neighboursNotLoaded" },
  });
}

/** Shortest paths between two nodes. */
export function usePaths() {
  const { api } = useTenantSession();
  return useMutation({
    mutationFn: (request: PathsRequest) => api.paths(request),
    meta: { silent: true }, // the Path view shows the error itself
  });
}

/** Saves edited properties with `If-Match`; a `412` means someone else saved first. */
export function useSaveNode() {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  return useMutation({
    meta: { success: "notices.saved" },
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
    meta: { success: "notices.nodeDeleted" },
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
    meta: { success: "notices.nodeCreated" },
    mutationFn: (node: NodeUpsert) => api.upsertNode(node),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.stats(tenant) }),
  });
}

/**
 * Adds labels, relationship types, parents or search indexes (admin). The
 * schema, the map and the property catalogue are read again afterwards.
 */
export function useAddToSchema() {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  return useMutation({
    meta: { success: "notices.schemaUpdated", errorTitle: "errors.schemaNotUpdated" },
    mutationFn: (addition: SchemaAddition) => api.addToSchema(addition),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: keys.schema(tenant) }),
        client.invalidateQueries({ queryKey: ["map", tenant] }),
        client.invalidateQueries({ queryKey: keys.catalogue(tenant) }),
      ]),
  });
}

/** Removes one declared label pair; the schema and its views refresh. */
export function useRemoveEdgeEndpoint() {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  return useMutation({
    meta: { success: "notices.schemaUpdated", errorTitle: "errors.schemaNotUpdated" },
    mutationFn: ({ type, src, dst }: { type: string; src: string; dst: string }) =>
      api.removeEdgeEndpoint(type, src, dst),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.schema(tenant) }),
  });
}

/** Creates an edge between two existing nodes. */
export function useCreateEdge() {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  return useMutation({
    meta: { success: "notices.relationshipCreated" },
    mutationFn: (edge: EdgeUpsert) => api.upsertEdge(edge),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.stats(tenant) }),
  });
}

/** Deletes one edge. */
export function useDeleteEdge() {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  return useMutation({
    meta: { success: "notices.relationshipDeleted" },
    mutationFn: (edge: EdgeKey & { etag?: string }) => api.deleteEdge(edge, edge.etag),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.stats(tenant) }),
  });
}

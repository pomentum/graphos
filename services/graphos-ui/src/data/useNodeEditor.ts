import type { EdgeKey } from "../api/graphosApi";
import type { EdgeRecord, NodeRecord } from "../api/types";
import { useTenantSession } from "../session/SessionProvider";
import { useActivity } from "./ActivityProvider";
import { useEdge, useNode, useSaveEdge, useSaveNode } from "./queries";
import { useVersionedEditor, type VersionedEditor } from "./useVersionedEditor";

/** Safe editing of one node's properties (see `useVersionedEditor`). */
export function useNodeEditor(ref: { id: string; label: string }): VersionedEditor<NodeRecord> {
  const { me } = useTenantSession();
  const { changes } = useActivity();
  const node = useNode(ref);
  const save = useSaveNode();
  const last = changes.find(
    (c) => c.kind === "node" && c.label === ref.label && c.ids?.includes(ref.id) && c.consumer !== me.consumer,
  );
  return useVersionedEditor({
    latest: node.data,
    isLoading: node.isLoading,
    refetch: () => node.refetch(),
    write: async (patch, etag) => (await save.mutateAsync({ ...ref, patch, etag })).data,
    saving: save.isPending,
    changedBy: last?.consumer ?? null,
  });
}

/** Safe editing of one edge's properties (see `useVersionedEditor`). */
export function useEdgeEditor(edge: EdgeKey): VersionedEditor<EdgeRecord> {
  const { me } = useTenantSession();
  const { changes } = useActivity();
  const record = useEdge(edge);
  const save = useSaveEdge();
  const last = changes.find(
    (c) =>
      c.kind === "edge" &&
      c.label === edge.type &&
      c.edges?.some(([src, dst]) => src === edge.srcId && dst === edge.dstId) &&
      c.consumer !== me.consumer,
  );
  return useVersionedEditor({
    latest: record.data,
    isLoading: record.isLoading,
    refetch: () => record.refetch(),
    write: async (patch, etag) => (await save.mutateAsync({ edge, patch, etag })).data,
    saving: save.isPending,
    changedBy: last?.consumer ?? null,
  });
}

import type { Properties } from "../../api/types";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { useTenantSession } from "../../session/SessionProvider";

/** At most this many nodes are put on the canvas at once (graphos looks up edges among up to 1,000). */
export const MAX_OPEN_NODES = 500;

/**
 * Puts a set of nodes of one label, and the relationships among them, on
 * the graph canvas as one step (Back takes them off), beside whatever is
 * there already, then shows the graph framed on them. Used by the map
 * (a label's hubs) and by Browse (the rows picked).
 */
export function useOpenNodes(): (
  title: string,
  label: string,
  nodes: { id: string; properties: Properties }[],
) => Promise<string[]> {
  const { api } = useTenantSession();
  const { model, select, setMode, frameOn } = useWorkspace();

  return async (title, label, nodes) => {
    const picked = nodes.slice(0, MAX_OPEN_NODES);
    const induced = picked.length > 1 ? await api.inducedEdges(picked.map((n) => n.id)) : { edges: [] };
    const keys = model.addGroup(
      title,
      picked.map((n) => ({ ref: { id: n.id, label }, properties: n.properties })),
      induced.edges.map((e) => ({
        type: e.type,
        src: { id: e.src_id, label },
        dst: { id: e.dst_id, label },
        properties: e.properties,
      })),
    );
    select(null);
    setMode("graph");
    frameOn(keys);
    return keys;
  };
}

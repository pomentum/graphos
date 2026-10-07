import { notifications } from "@mantine/notifications";
import { useMemo, useState } from "react";
import type { NeighborsResponse } from "../../api/types";
import type { ExpandDialogProps } from "../../components/ExpandDialog";
import { useExpand } from "../../data/queries";
import { countByEdgeType, EXPAND_FETCH_LIMIT, needsChoice } from "../../lib/expand";
import { displayName, type GraphModel } from "../../graph/GraphModel";

interface Pending {
  key: string;
  response: NeighborsResponse;
}

/**
 * Adds a node's neighbours to the canvas. Few neighbours are added at once;
 * many (or more than graphos returned) open `ExpandDialog` to choose edge
 * types first. Returns `expand` and the props for the dialog.
 */
export function useExpandNode(model: GraphModel): {
  expand: (key: string) => Promise<void>;
  dialog: ExpandDialogProps;
  isExpanding: boolean;
} {
  const fetchNeighbors = useExpand();
  const [pending, setPending] = useState<Pending | null>(null);

  const add = (key: string, response: NeighborsResponse) => {
    const added = model.addNeighbors(key, response.neighbors);
    if (added.length === 0) notifications.show({ message: "No neighbours", color: "gray" });
  };

  // A failed fetch is shown by the query client; nothing is added.
  const expand = async (key: string) => {
    const { id } = model.graph.getNodeAttributes(key);
    const response = await fetchNeighbors.mutateAsync({ id, limit: EXPAND_FETCH_LIMIT }).catch(() => null);
    if (!response) return;
    if (needsChoice(response.neighbors, response.truncated)) setPending({ key, response });
    else add(key, response);
  };

  const confirm = async (edgeTypes: string[]) => {
    if (!pending) return;
    const { key, response } = pending;
    setPending(null);
    // All neighbours were fetched: filter them here. Otherwise ask graphos for just these types.
    const narrowed = response.truncated
      ? await fetchNeighbors
          .mutateAsync({
            id: model.graph.getNodeAttribute(key, "id"),
            relTypes: edgeTypes,
            limit: EXPAND_FETCH_LIMIT,
          })
          .catch(() => null)
      : { ...response, neighbors: response.neighbors.filter((n) => edgeTypes.includes(n.edge_type)) };
    if (narrowed) add(key, narrowed);
  };

  const node = pending ? model.graph.getNodeAttributes(pending.key) : null;
  // Memoised: a new array on every render would reset the dialog's ticks.
  const counts = useMemo(() => (pending ? countByEdgeType(pending.response.neighbors) : []), [pending]);
  return {
    expand,
    isExpanding: fetchNeighbors.isPending,
    dialog: {
      opened: pending !== null,
      nodeName: node ? displayName(node.properties, node.id) : "",
      counts,
      truncated: pending?.response.truncated ?? false,
      onCancel: () => setPending(null),
      onConfirm: (types) => void confirm(types),
    },
  };
}

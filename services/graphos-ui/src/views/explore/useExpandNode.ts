import { notifications } from "@mantine/notifications";
import { useMemo, useRef, useState } from "react";
import type { Neighbor, NeighborsResponse } from "../../api/types";
import type { ExpandDialogProps } from "../../components/ExpandDialog";
import { useExpand } from "../../data/queries";
import { countByEdgeType, EXPAND_FETCH_LIMIT } from "../../lib/expand";
import { displayName, nodeKey, type GraphModel } from "../../graph/GraphModel";

/** Neighbours added per expand (and per "+N more"); few enough to label and follow. */
export const EXPAND_PAGE = 20;

interface Pending {
  key: string;
  response: NeighborsResponse;
}

/**
 * Adds a node's neighbours to the canvas, a page at a time: the first
 * `EXPAND_PAGE`, then a "+N more" node that adds the next page when opened.
 * When graphos has more neighbours than one fetch returns, `ExpandDialog`
 * asks which relationship types to follow first. `onExpanded` hears which
 * node was expanded and which nodes it added, so the view can point at them.
 */
export function useExpandNode(
  model: GraphModel,
  onExpanded: (center: string, added: string[]) => void,
): {
  expand: (key: string) => Promise<void>;
  dialog: ExpandDialogProps;
  isExpanding: boolean;
} {
  const fetchNeighbors = useExpand();
  const [pending, setPending] = useState<Pending | null>(null);
  /**
   * The neighbours fetched for each expanded node. Which are still to add is
   * worked out from the canvas each time, so Back and Hide put nodes back in
   * the queue.
   */
  const fetched = useRef(new Map<string, Neighbor[]>());

  /** Adds the next page of `center`'s neighbours that are not on the canvas. */
  const addPage = (center: string) => {
    const queue = (fetched.current.get(center) ?? []).filter(
      (n) => !model.graph.hasNode(nodeKey({ id: n.neighbor_id, label: n.neighbor_label })),
    );
    const page = queue.slice(0, EXPAND_PAGE);
    const added = model.addNeighbors(center, page, queue.length - page.length);
    onExpanded(center, added);
  };

  /** Starts paging through `neighbors` of `center`, edges to nodes already shown included. */
  const begin = (center: string, neighbors: Neighbor[]) => {
    const shown = neighbors.filter((n) => model.graph.hasNode(nodeKey({ id: n.neighbor_id, label: n.neighbor_label })));
    if (neighbors.length === 0) {
      notifications.show({ message: "No neighbours", color: "gray" });
      return;
    }
    // Edges to neighbours already on the canvas are drawn at once; new ones page.
    if (shown.length > 0) model.addNeighbors(center, shown);
    if (shown.length === neighbors.length) {
      notifications.show({ message: "Its neighbours are all on the canvas already", color: "gray" });
      onExpanded(center, []);
      return;
    }
    fetched.current.set(center, neighbors);
    addPage(center);
  };

  // A failed fetch is shown by the query client; nothing is added.
  const expand = async (key: string) => {
    if (!model.graph.hasNode(key)) return;
    const node = model.graph.getNodeAttributes(key);
    if (node.more !== undefined) {
      // A "+N more" node: the next page of the node it hangs off.
      const center = model.graph.neighbors(key)[0];
      if (center) addPage(center);
      return;
    }
    const response = await fetchNeighbors.mutateAsync({ id: node.id, limit: EXPAND_FETCH_LIMIT }).catch(() => null);
    if (!response) return;
    if (response.truncated) setPending({ key, response });
    else begin(key, response.neighbors);
  };

  const confirm = async (edgeTypes: string[]) => {
    if (!pending) return;
    const { key } = pending;
    setPending(null);
    const narrowed = await fetchNeighbors
      .mutateAsync({ id: model.graph.getNodeAttribute(key, "id"), relTypes: edgeTypes, limit: EXPAND_FETCH_LIMIT })
      .catch(() => null);
    if (narrowed) begin(key, narrowed.neighbors);
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

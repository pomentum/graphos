import { notifications } from "@mantine/notifications";
import { useState } from "react";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { t } from "../../i18n";
import { useTenantSession } from "../../session/SessionProvider";
import { MAX_OPEN_NODES, useOpenNodes } from "./useOpenNodes";

/** At most this many nodes are reached by one multi-hop expand. */
export const HOPS_LIMIT = 200;

/**
 * Expands a node several hops at once: every node within `hops` hops, in
 * either direction, and the relationships among them and the node, go on
 * the canvas as one step (Back takes them off).
 */
export function useExpandHops(): { expandHops: (key: string, hops: number) => Promise<void>; expanding: boolean } {
  const { api } = useTenantSession();
  const { model } = useWorkspace();
  const openNodes = useOpenNodes();
  const [expanding, setExpanding] = useState(false);

  const expandHops = async (key: string, hops: number) => {
    if (!model.graph.hasNode(key)) return;
    const seed = model.graph.getNodeAttributes(key);
    setExpanding(true);
    try {
      const reached = await api.expandHops(seed.id, hops, Math.min(HOPS_LIMIT, MAX_OPEN_NODES - 1));
      if (reached.paths.length === 0) {
        notifications.show({ message: t("notices.noNeighbours"), color: "gray" });
        return;
      }
      await openNodes(t("views.explore.history.expandHops", { name: seed.id, hops }), seed.vertexLabel, [
        { id: seed.id, label: seed.vertexLabel, properties: seed.properties },
        ...reached.paths.map((p) => ({ id: p.end_id, label: p.end_label, properties: p.end_properties })),
      ]);
      if (reached.truncated)
        notifications.show({ message: t("notices.hopsTruncated", { count: reached.paths.length }), color: "gray" });
    } catch (error) {
      notifications.show({
        color: "red",
        title: t("errors.expandFailed"),
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setExpanding(false);
    }
  };
  return { expandHops, expanding };
}

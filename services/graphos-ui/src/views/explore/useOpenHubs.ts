import { notifications } from "@mantine/notifications";
import { useState } from "react";
import { t } from "../../i18n";
import { useTenantSession } from "../../session/SessionProvider";
import { useOpenNodes } from "./useOpenNodes";

/** Hubs put on the canvas when a label is opened from the map. */
export const HUBS_PER_LABEL = 40;

/**
 * Opens a label from the map: its best-connected nodes and the edges among
 * them go on the graph canvas as one step (Back takes them off), beside
 * whatever is there already, and Explore switches to the graph to show them.
 */
export function useOpenHubs(): { openHubs: (label: string) => Promise<void>; opening: string | null } {
  const { api } = useTenantSession();
  const openNodes = useOpenNodes();
  const [opening, setOpening] = useState<string | null>(null);

  const openHubs = async (label: string) => {
    setOpening(label);
    try {
      const hubs = await api.hubs(label, HUBS_PER_LABEL);
      if (hubs.nodes.length === 0) {
        notifications.show({ message: t("notices.noHubs", { label }), color: "gray" });
        return;
      }
      await openNodes(t("views.explore.history.open", { name: label }), label, hubs.nodes);
    } catch (error) {
      notifications.show({
        color: "red",
        title: t("errors.openLabelFailed", { label }),
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setOpening(null);
    }
  };
  return { openHubs, opening };
}

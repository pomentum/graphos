import { notifications } from "@mantine/notifications";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { keys } from "../../data/queries";
import { t } from "../../i18n";
import { useTenantSession } from "../../session/SessionProvider";

/** How often a running recompute is checked. */
const POLL_MS = 3_000;

/**
 * Recomputes the groups (admin) as a background job and waits for it,
 * then reloads the group map. Recomputing a large graph takes minutes; the
 * page stays usable meanwhile.
 */
export function useRecomputeGroups(): { recompute: () => Promise<void>; running: boolean } {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  const [running, setRunning] = useState(false);

  const recompute = async () => {
    setRunning(true);
    try {
      let job = await api.recomputeGroups();
      while (job.status === "queued" || job.status === "running") {
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        job = await api.job(job.id);
      }
      if (job.status !== "succeeded") throw new Error(job.error?.message ?? job.status);
      await client.invalidateQueries({ queryKey: keys.groups(tenant) });
    } catch (error) {
      notifications.show({
        color: "red",
        title: t("views.explore.groups.computeFailed"),
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setRunning(false);
    }
  };
  return { recompute, running };
}

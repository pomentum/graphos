import { notifications } from "@mantine/notifications";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import type { CommunityAlgorithm } from "../../api/types";
import { isActiveJob, keys, useGroupsJob } from "../../data/queries";
import { t } from "../../i18n";
import { useTenantSession } from "../../session/SessionProvider";

/**
 * Recomputing the groups (admin): starts the background job, and follows
 * whichever recompute of this tenant is running, whoever started it and
 * from wherever. The job is read from graphos, not kept in the page, so
 * leaving the view, reloading, or a recompute started elsewhere are all
 * seen; when it ends the group map is read again.
 */
export function useRecomputeGroups(): {
  recompute: (options?: { algorithm?: CommunityAlgorithm; fromScratch?: boolean }) => Promise<void>;
  running: boolean;
} {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  const job = useGroupsJob();
  const running = isActiveJob(job.data);

  // A recompute that was running and has now ended.
  const wasRunning = useRef(running);
  useEffect(() => {
    if (wasRunning.current && !running && job.data) {
      if (job.data.status === "succeeded") {
        void client.invalidateQueries({ queryKey: keys.groups(tenant) });
        notifications.show({ message: t("views.explore.groups.computeDone") });
      } else {
        notifications.show({
          color: "red",
          title: t("views.explore.groups.computeFailed"),
          message: job.data.error?.detail ?? job.data.status,
        });
      }
    }
    wasRunning.current = running;
  }, [running, job.data, client, tenant]);

  const recompute = async (options: { algorithm?: CommunityAlgorithm; fromScratch?: boolean } = {}) => {
    try {
      await api.recomputeGroups(options);
    } catch (error) {
      notifications.show({
        color: "red",
        title: t("views.explore.groups.computeFailed"),
        message: error instanceof Error ? error.message : String(error),
      });
    }
    await client.invalidateQueries({ queryKey: keys.groupsJob(tenant) });
  };
  return { recompute, running };
}

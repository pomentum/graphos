import { notifications } from "@mantine/notifications";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { isActiveJob, keys, useMetricsJob } from "../../data/queries";
import { t } from "../../i18n";
import { useTenantSession } from "../../session/SessionProvider";

/**
 * Computing graph metrics (`maintain`): starts the background job and
 * follows whichever metrics run of this tenant is going, whoever started it,
 * like the groups recompute. The job is read from graphos, so leaving the
 * page or a run started elsewhere are seen too.
 */
export function useComputeMetrics(): { compute: (metrics: string[]) => Promise<void>; running: boolean } {
  const { api, tenant } = useTenantSession();
  const client = useQueryClient();
  const job = useMetricsJob();
  const running = isActiveJob(job.data);

  // A run that was going and has now ended.
  const wasRunning = useRef(running);
  useEffect(() => {
    if (wasRunning.current && !running && job.data) {
      if (job.data.status === "succeeded") {
        // The metric views read the stored values: read them again.
        for (const view of ["topByMetric", "components", "component"]) {
          void client.invalidateQueries({ queryKey: [view, tenant] });
        }
        notifications.show({ message: t("views.overview.algorithms.metricsDone") });
      } else {
        notifications.show({
          color: "red",
          title: t("views.overview.algorithms.metricsFailed"),
          message: job.data.error?.detail ?? job.data.status,
        });
      }
    }
    wasRunning.current = running;
  }, [running, job.data, client, tenant]);

  const compute = async (metrics: string[]) => {
    try {
      await api.computeMetrics(metrics);
    } catch (error) {
      notifications.show({
        color: "red",
        title: t("views.overview.algorithms.metricsFailed"),
        message: error instanceof Error ? error.message : String(error),
      });
    }
    await client.invalidateQueries({ queryKey: keys.metricsJob(tenant) });
  };
  return { compute, running };
}

import { Button, Checkbox, Group, Stack, Text, Title } from "@mantine/core";
import type { TFunction } from "i18next";
import { IconRefresh } from "@tabler/icons-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { Job } from "../../api/types";
import { useCommunityMap, useMetricsJob, useServiceInfo } from "../../data/queries";
import { groupsOffer } from "../../lib/features";
import { DEFAULT_METRICS, METRICS } from "../../lib/metrics";
import { relativeTime } from "../../lib/time";
import { useTenantSession } from "../../session/SessionProvider";
import { useRecomputeGroups } from "../explore/useRecomputeGroups";
import classes from "./OverviewView.module.css";
import { useComputeMetrics } from "./useComputeMetrics";

/**
 * The whole-graph computations of a tenant in one place: the groups (Louvain
 * communities) and the graph metrics, each with what its last run found and,
 * for whoever maintains the tenant, a button to run it. Groups are offered
 * only where the server has the Louvain extra. One runs at a time per tenant,
 * so both wait while either is running.
 */
export function GraphAlgorithms() {
  const { t } = useTranslation();
  const { canMaintain } = useTenantSession();
  const info = useServiceInfo();
  const map = useCommunityMap();
  const metricsJob = useMetricsJob();
  const groups = useRecomputeGroups();
  const metrics = useComputeMetrics();
  const [betweenness, setBetweenness] = useState(false);

  const offer = groupsOffer(canMaintain, info.data?.features.louvain);
  const busy = groups.running || metrics.running;
  const computed = map.data?.available ? map.data : null;

  const groupsStatus = groups.running
    ? t("views.overview.algorithms.running")
    : computed
      ? t("views.overview.algorithms.groupsStatus", {
          count: computed.n_communities,
          modularity: computed.modularity?.toFixed(2) ?? "–",
          when: computed.computed_at ? relativeTime(computed.computed_at) : "",
        })
      : t("views.overview.algorithms.notComputed");

  return (
    <Stack gap="xs">
      <Title order={2} size="sm">
        {t("views.overview.algorithms.title")}
      </Title>
      <Text size="xs" c="dimmed">
        {t("views.overview.algorithms.hint")}
      </Text>
      <div className={classes.algorithms}>
        <Algorithm
          name={t("views.overview.algorithms.groups")}
          what={t("views.overview.algorithms.groupsWhat")}
          status={groupsStatus}
          action={
            offer === "allowed" ? (
              <Button
                size="xs"
                variant="default"
                leftSection={<IconRefresh size={14} />}
                loading={groups.running}
                disabled={busy && !groups.running}
                onClick={() => void groups.recompute()}
              >
                {computed ? t("views.overview.algorithms.computeAgain") : t("views.overview.algorithms.compute")}
              </Button>
            ) : offer === "notInstalled" ? (
              <Text size="xs" c="dimmed" maw={260}>
                {t("views.overview.algorithms.louvainMissing")}
              </Text>
            ) : null
          }
        />
        <Algorithm
          name={t("views.overview.algorithms.metrics")}
          what={t("views.overview.algorithms.metricsWhat")}
          status={metricsStatus(metricsJob.data ?? null, metrics.running, t)}
          action={
            canMaintain ? (
              <Group gap="sm" wrap="nowrap">
                <Checkbox
                  size="xs"
                  label={t("views.overview.algorithms.betweenness")}
                  checked={betweenness}
                  onChange={(e) => setBetweenness(e.currentTarget.checked)}
                />
                <Button
                  size="xs"
                  variant="default"
                  leftSection={<IconRefresh size={14} />}
                  loading={metrics.running}
                  disabled={busy && !metrics.running}
                  onClick={() => void metrics.compute([...DEFAULT_METRICS, ...(betweenness ? ["betweenness"] : [])])}
                >
                  {t("views.overview.algorithms.compute")}
                </Button>
              </Group>
            ) : null
          }
        />
      </div>
      {busy ? (
        <Text size="xs" c="dimmed">
          {t("views.overview.algorithms.oneAtATime")}
        </Text>
      ) : null}
    </Stack>
  );
}

function Algorithm({ name, what, status, action }: { name: string; what: string; status: string; action: ReactNode }) {
  return (
    <Group justify="space-between" align="center" wrap="wrap" gap="sm" className={classes.algorithm}>
      <div>
        <Text size="sm" fw={600}>
          {name}
        </Text>
        <Text size="xs" c="dimmed">
          {what}
        </Text>
        <Text size="xs" mt={4}>
          {status}
        </Text>
      </div>
      {action}
    </Group>
  );
}

/** What the last metrics run did, from its job (kept for the job retention, a week by default). */
function metricsStatus(job: Job | null, running: boolean, t: TFunction): string {
  if (running) return t("views.overview.algorithms.running");
  if (!job) return t("views.overview.algorithms.noRecentRun");
  if (job.status !== "succeeded") {
    return t("views.overview.algorithms.lastFailed", { detail: job.error?.detail ?? job.status });
  }
  const result = job.result ?? {};
  const ran = Array.isArray(result.metrics) ? (result.metrics as string[]) : [];
  const names = METRICS.filter((m) => ran.includes(m)).map((m) => t(`views.overview.algorithms.metricNames.${m}`));
  return t("views.overview.algorithms.metricsStatus", {
    metrics: names.join(", "),
    nodes: typeof result.n_nodes === "number" ? result.n_nodes : 0,
    when: job.finished_at ? relativeTime(job.finished_at) : "",
  });
}

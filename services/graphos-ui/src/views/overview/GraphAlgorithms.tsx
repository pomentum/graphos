import { Button, Checkbox, Group, MultiSelect, SegmentedControl, Stack, Text, Title } from "@mantine/core";
import type { TFunction } from "i18next";
import { IconRefresh } from "@tabler/icons-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { CommunityAlgorithm, Job } from "../../api/types";
import { useCommunityMap, useGroupsJob, useMetricsJob, useSchema, useServiceInfo } from "../../data/queries";
import { communityAlgorithms, groupsOffer, groupsRun } from "../../lib/features";
import { DEFAULT_METRICS, METRICS } from "../../lib/metrics";
import { relativeTime } from "../../lib/time";
import { useTenantSession } from "../../session/SessionProvider";
import { useRecomputeGroups } from "../explore/useRecomputeGroups";
import classes from "./OverviewView.module.css";
import { useComputeMetrics } from "./useComputeMetrics";

/**
 * The whole-graph computations of a tenant in one place: the groups (Louvain
 * or Leiden communities) and the graph metrics, each with what its last run
 * found and, for whoever maintains the tenant, a button to run it. Groups are
 * offered only where the server has the Louvain extra; the algorithm is the
 * tenant's unless another is chosen, and the choice is offered only by a
 * server that has more than one (0.14.0 on). The metrics run over chosen
 * relationship types, or all of them: PageRank over dependencies says what
 * everything relies on, over every relationship what is most connected. One
 * runs at a time per tenant, so both wait while either is running.
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
  const [relTypes, setRelTypes] = useState<string[]>([]);
  const schema = useSchema().data;
  const edgeLabels = schema?.edge_labels ?? [];
  const groupsJob = useGroupsJob();
  const [choice, setChoice] = useState<"default" | CommunityAlgorithm>("default");
  const [fromScratch, setFromScratch] = useState(false);

  const offer = groupsOffer(canMaintain, info.data?.features.louvain);
  const busy = groups.running || metrics.running;
  const computed = map.data?.available ? map.data : null;
  const algorithms = communityAlgorithms(info.data?.features);
  const tenantDefault: CommunityAlgorithm = schema?.community_algorithm ?? "louvain";
  const chosen = choice === "default" ? tenantDefault : choice;
  const algorithmName = (a: CommunityAlgorithm) => t(`views.overview.algorithms.algorithmNames.${a}`);
  const lastRun = groupsRun(groupsJob.data);

  const groupsStatus = groups.running
    ? t("views.overview.algorithms.running")
    : computed
      ? [
          t("views.overview.algorithms.groupsStatus", {
            count: computed.n_communities,
            modularity: computed.modularity?.toFixed(2) ?? "–",
            when: computed.computed_at ? relativeTime(computed.computed_at) : "",
          }),
          ...(lastRun
            ? [
                lastRun.fromScratch
                  ? t("views.overview.algorithms.ranFromScratch", { algorithm: algorithmName(lastRun.algorithm) })
                  : t("views.overview.algorithms.ranFromStored", { algorithm: algorithmName(lastRun.algorithm) }),
              ]
            : []),
        ].join(" · ")
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
          note={
            schema?.community_algorithm
              ? t("views.overview.algorithms.tenantDefault", { algorithm: algorithmName(tenantDefault) })
              : undefined
          }
          action={
            offer === "allowed" ? (
              <Stack gap={8} align="flex-end">
                {algorithms.length > 1 ? (
                  <SegmentedControl
                    size="xs"
                    aria-label={t("views.overview.algorithms.algorithm")}
                    value={choice}
                    onChange={(value) => setChoice(value as "default" | CommunityAlgorithm)}
                    data={[
                      {
                        value: "default",
                        label: t("views.overview.algorithms.algorithmDefault", {
                          algorithm: algorithmName(tenantDefault),
                        }),
                      },
                      ...algorithms.map((a) => ({ value: a, label: algorithmName(a) })),
                    ]}
                  />
                ) : null}
                <Group gap="sm" wrap="nowrap">
                  {chosen === "leiden" ? (
                    <Checkbox
                      size="xs"
                      label={t("views.overview.algorithms.fromScratch")}
                      checked={fromScratch}
                      onChange={(e) => setFromScratch(e.currentTarget.checked)}
                    />
                  ) : null}
                  <Button
                    size="xs"
                    variant="default"
                    leftSection={<IconRefresh size={14} />}
                    loading={groups.running}
                    disabled={busy && !groups.running}
                    onClick={() =>
                      void groups.recompute({
                        algorithm: choice === "default" ? undefined : choice,
                        fromScratch: chosen === "leiden" && fromScratch,
                      })
                    }
                  >
                    {computed ? t("views.overview.algorithms.computeAgain") : t("views.overview.algorithms.compute")}
                  </Button>
                </Group>
              </Stack>
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
              <Stack gap={8} align="flex-end">
                <MultiSelect
                  size="xs"
                  w={300}
                  label={t("views.overview.algorithms.relTypes")}
                  placeholder={relTypes.length === 0 ? t("views.overview.algorithms.allRelTypes") : undefined}
                  data={edgeLabels}
                  value={relTypes}
                  onChange={setRelTypes}
                  searchable
                  clearable
                />
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
                    onClick={() =>
                      void metrics.compute([...DEFAULT_METRICS, ...(betweenness ? ["betweenness"] : [])], relTypes)
                    }
                  >
                    {t("views.overview.algorithms.compute")}
                  </Button>
                </Group>
              </Stack>
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

function Algorithm({
  name,
  what,
  status,
  note,
  action,
}: {
  name: string;
  what: string;
  status: string;
  note?: string;
  action: ReactNode;
}) {
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
        {note ? (
          <Text size="xs" c="dimmed">
            {note}
          </Text>
        ) : null}
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
  const types = Array.isArray(job.params.rel_types) ? (job.params.rel_types as string[]) : [];
  const names = METRICS.filter((m) => ran.includes(m)).map((m) => t(`views.overview.algorithms.metricNames.${m}`));
  return t("views.overview.algorithms.metricsStatus", {
    metrics: names.join(", "),
    nodes: typeof result.n_nodes === "number" ? result.n_nodes : 0,
    over:
      types.length > 0
        ? t("views.overview.algorithms.overTypes", { types: types.join(", ") })
        : t("views.overview.algorithms.overAll"),
    when: job.finished_at ? relativeTime(job.finished_at) : "",
  });
}

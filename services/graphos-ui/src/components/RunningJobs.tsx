import { Button, Group, Loader, Popover, Stack, Text, UnstyledButton } from "@mantine/core";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import type { Job } from "../api/types";
import { useActiveJobs, useCancelJob } from "../data/queries";
import { canCancel, jobName, jobState } from "../lib/jobs";
import { useTenantSession } from "../session/SessionProvider";
import classes from "./RunningJobs.module.css";

/** The current time, again every `ms`: for durations that keep counting. */
export function useNow(ms = 1_000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), ms);
    return () => window.clearInterval(timer);
  }, [ms]);
  return now;
}

/**
 * What runs on the tenant now, one row each: the job, who started it, for how
 * long, whether its caller waits (a direct call) or not (a background job),
 * and Cancel where this session may cancel it.
 */
export function RunningJobList({ jobs }: { jobs: Job[] }) {
  const { t } = useTranslation();
  const session = useTenantSession();
  const cancel = useCancelJob();
  const now = useNow();
  return (
    <div className={classes.list}>
      {jobs.map((job) => {
        const name = jobName(job.kind);
        return (
          <div key={job.id} className={classes.row}>
            <Loader size={14} color={job.cancel_requested ? "gray" : "accent"} />
            <div className={classes.text}>
              <Text size="sm" fw={600} truncate>
                {name}
              </Text>
              <Text size="xs" c="dimmed" truncate>
                {[
                  job.consumer ?? t("common.unknownConsumer"),
                  jobState(job, now),
                  job.sync ? t("common.jobs.direct") : t("common.jobs.background"),
                ].join(" · ")}
              </Text>
            </div>
            {canCancel(job, session) ? (
              <Button
                size="compact-xs"
                variant="subtle"
                color="red"
                loading={cancel.isPending && cancel.variables?.id === job.id}
                onClick={() => cancel.mutate(job)}
                aria-label={t("common.jobs.cancelOne", { job: name })}
              >
                {t("common.jobs.cancel")}
              </Button>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

/** In the toolbar while something runs on the tenant: a spinner and how many, with the list on click. */
export function RunningIndicator() {
  const { t } = useTranslation();
  const jobs = useActiveJobs().data ?? [];
  if (jobs.length === 0) return null;
  const label = t("shell.running.label", { count: jobs.length });
  return (
    <Popover position="bottom-end" width={340} radius="lg">
      <Popover.Target>
        <UnstyledButton className={classes.pill} aria-label={label} title={label}>
          <Loader size={12} color="accent" />
          <Text size="xs" fw={600}>
            {jobs.length}
          </Text>
        </UnstyledButton>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap={6}>
          <Group justify="space-between">
            <Text size="xs" c="dimmed">
              {t("shell.running.title")}
            </Text>
            <Text component={Link} to="/activity" size="xs" c="var(--gx-accent-text)">
              {t("shell.running.showAll")}
            </Text>
          </Group>
          <RunningJobList jobs={jobs} />
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}

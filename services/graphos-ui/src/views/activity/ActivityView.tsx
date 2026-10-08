import { Avatar, Badge, Button, Group, SegmentedControl, Select, Text, UnstyledButton } from "@mantine/core";
import { IconChevronRight } from "@tabler/icons-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import type { ChangeItem, Job } from "../../api/types";
import { RunningJobList } from "../../components/RunningJobs";
import { useActivity } from "../../data/ActivityProvider";
import { useActiveJobs, useChangeHistory, useRecentJobs } from "../../data/queries";
import { useWorkspace } from "../../graph/WorkspaceProvider";
import { useTenantSession } from "../../session/SessionProvider";
import {
  type ActivityCategory,
  changeCategory,
  describeChange,
  openableNode,
  streamStatusText,
} from "../../lib/changes";
import { isActiveJob, jobName, jobOutcome } from "../../lib/jobs";
import { relativeTime } from "../../lib/time";
import classes from "./ActivityView.module.css";

type Category = ActivityCategory | "all";
const CATEGORIES: Category[] = ["all", "writes", "computations", "schema"];

/** One row of the timeline: a change from the feed, or a job that ended. */
type Entry =
  | { type: "change"; key: string; time: number; who: string; change: ChangeItem }
  | { type: "job"; key: string; time: number; who: string; job: Job };

/**
 * Activity: what happens on the tenant and who does it. On top, what runs now
 * (background jobs and direct calls, with Cancel where allowed); below, the
 * writes, computations and schema changes of the last days, live, newest
 * first, with older ones a page at a time. A change to one node that still
 * exists opens it in Explore (those rows show a chevron).
 */
export function ActivityView() {
  const { t } = useTranslation();
  const unknown = t("common.unknownConsumer");
  const { changes: live, status, markSeen } = useActivity();
  const { api } = useTenantSession();
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const [category, setCategory] = useState<Category>("all");
  const [person, setPerson] = useState<string | null>(null);
  const [label, setLabel] = useState<string | null>(null);
  const history = useChangeHistory(category);
  const active = useActiveJobs().data ?? [];
  const recent = useRecentJobs().data;

  // Everything shown here is seen, including changes that arrive while watching.
  const newest = live[0]?.cursor;
  useEffect(markSeen, [markSeen, newest]);

  const entries = useMemo<Entry[]>(() => {
    const seen = new Set<string>();
    const out: Entry[] = [];
    for (const change of [...live, ...(history.data?.pages.flatMap((page) => page.changes) ?? [])]) {
      if (seen.has(change.cursor)) continue;
      seen.add(change.cursor);
      if (category !== "all" && changeCategory(change) !== category) continue;
      const time = Date.parse(change.at);
      out.push({ type: "change", key: change.cursor, time, who: change.consumer ?? unknown, change });
    }
    if (category === "all" || category === "computations") {
      // Jobs older than the oldest change loaded come with the page that reaches them.
      const oldest = history.hasNextPage && out.length ? Math.min(...out.map((entry) => entry.time)) : -Infinity;
      for (const job of recent ?? []) {
        if (isActiveJob(job) || !job.finished_at) continue;
        const time = Date.parse(job.finished_at);
        if (time < oldest) continue;
        out.push({ type: "job", key: job.id, time, who: job.consumer ?? unknown, job });
      }
    }
    return out.sort((a, b) => b.time - a.time);
  }, [live, history.data, history.hasNextPage, recent, category, unknown]);

  const people = useMemo(() => unique(entries.map((entry) => entry.who)), [entries]);
  const labels = useMemo(
    () =>
      unique(entries.flatMap((entry) => (entry.type === "change" && entry.change.label ? [entry.change.label] : []))),
    [entries],
  );
  const shown = entries.filter(
    (entry) =>
      (!person || entry.who === person) && (!label || (entry.type === "change" && entry.change.label === label)),
  );

  /** Opens the node, then fills in its properties (the change carries only its id). */
  const open = (change: ChangeItem) => {
    const ref = openableNode(change);
    if (!ref) return;
    const key = workspace.open(ref, { id: ref.id });
    void api
      .node(ref.id, ref.label)
      .then((record) => workspace.model.updateNode(key, record.properties))
      .catch(() => undefined);
    void navigate("/explore");
  };
  const filtered = Boolean(person || label);

  return (
    <div className={classes.root}>
      <Group justify="space-between" mb="md" gap="sm">
        <Badge variant="dot" color={status === "live" ? "accent" : "gray"} size="lg" radius="sm" tt="none">
          {streamStatusText(status)}
        </Badge>
        <Group gap="xs">
          <Select
            size="xs"
            placeholder={t("views.activity.everyone")}
            data={people}
            value={person}
            onChange={setPerson}
            clearable
            w={150}
            aria-label={t("views.activity.byPerson")}
          />
          <Select
            size="xs"
            placeholder={t("views.activity.allLabels")}
            data={labels}
            value={label}
            onChange={setLabel}
            clearable
            w={150}
            aria-label={t("views.activity.byLabel")}
          />
        </Group>
      </Group>
      <SegmentedControl
        size="xs"
        mb="lg"
        fullWidth
        value={category}
        onChange={(value) => setCategory(value as Category)}
        aria-label={t("views.activity.category")}
        data={CATEGORIES.map((value) => ({ value, label: t(`views.activity.categories.${value}`) }))}
      />

      {active.length > 0 ? (
        <>
          <Text size="xs" fw={600} c="dimmed" tt="uppercase" mb={6}>
            {t("views.activity.running")}
          </Text>
          <div className={`${classes.group} ${classes.running}`}>
            <RunningJobList jobs={active} />
          </div>
          <Text size="xs" fw={600} c="dimmed" tt="uppercase" mt="lg" mb={6}>
            {t("views.activity.history")}
          </Text>
        </>
      ) : null}

      {shown.length === 0 ? (
        <div className={classes.empty}>
          <Text fw={600}>{filtered ? t("views.activity.noMatch") : t("views.activity.empty")}</Text>
          <Text size="sm" c="dimmed">
            {filtered ? t("views.activity.hidden", { count: entries.length }) : t("views.activity.emptyHint")}
          </Text>
          {filtered ? (
            <Button
              size="xs"
              variant="default"
              mt="sm"
              onClick={() => {
                setPerson(null);
                setLabel(null);
              }}
            >
              {t("views.activity.clearFilters")}
            </Button>
          ) : null}
        </div>
      ) : (
        <div className={classes.group}>
          {shown.map((entry) =>
            entry.type === "change" ? (
              <ChangeRow key={entry.key} change={entry.change} who={entry.who} onOpen={open} />
            ) : (
              <JobRow key={entry.key} job={entry.job} who={entry.who} />
            ),
          )}
        </div>
      )}

      <Group justify="center" mt="md">
        {history.isError ? (
          <Text size="sm" c="red">
            {t("views.activity.loadFailed")}
          </Text>
        ) : history.hasNextPage ? (
          <Button
            size="xs"
            variant="default"
            loading={history.isFetchingNextPage}
            onClick={() => void history.fetchNextPage()}
          >
            {t("views.activity.loadOlder")}
          </Button>
        ) : history.isSuccess && entries.length > 0 ? (
          <Text size="xs" c="dimmed">
            {t("views.activity.allLoaded")}
          </Text>
        ) : null}
      </Group>
    </div>
  );
}

function ChangeRow({ change, who, onOpen }: { change: ChangeItem; who: string; onOpen: (change: ChangeItem) => void }) {
  const { t } = useTranslation();
  const openable = openableNode(change) !== null;
  const Row = openable ? UnstyledButton : "div";
  const text = describeChange(change);
  return (
    <Row
      className={classes.row}
      data-openable={openable || undefined}
      onClick={openable ? () => onOpen(change) : undefined}
      title={openable ? t("common.actions.openInExplore") : undefined}
    >
      <Avatar size={28} radius="xl" color="gray">
        {(change.consumer ?? "?").slice(0, 2).toUpperCase()}
      </Avatar>
      <div className={classes.text}>
        <Text size="sm" truncate title={text}>
          <Text span fw={600}>
            {who}
          </Text>{" "}
          {text.replace(/^./, (c) => c.toLowerCase())}
        </Text>
        <Text size="xs" c="dimmed">
          {relativeTime(change.at)}
        </Text>
      </div>
      {openable ? <IconChevronRight size={14} className="gx-muted" /> : null}
    </Row>
  );
}

function JobRow({ job, who }: { job: Job; who: string }) {
  const { t } = useTranslation();
  const text = `${t("common.jobs.ran", { job: jobName(job.kind) })} · ${jobOutcome(job)}`;
  return (
    <div className={classes.row}>
      <Avatar size={28} radius="xl" color="gray">
        {(job.consumer ?? "?").slice(0, 2).toUpperCase()}
      </Avatar>
      <div className={classes.text}>
        <Text size="sm" truncate title={text} c={job.status === "failed" ? "red" : undefined}>
          <Text span fw={600} c="var(--mantine-color-text)">
            {who}
          </Text>{" "}
          {text}
        </Text>
        <Text size="xs" c="dimmed">
          {relativeTime(job.finished_at ?? job.created_at)}
        </Text>
      </div>
    </div>
  );
}

function unique(values: string[]): string[] {
  return [...new Set(values)].sort();
}

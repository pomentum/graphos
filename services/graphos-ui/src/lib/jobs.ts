import type { Job, Level } from "../api/types";
import { t } from "../i18n";
import { atLeast } from "../session/levels";

const KINDS = ["recompute_communities", "compute_metrics", "resolve_edges", "vacuum", "rename_property"] as const;
type KnownKind = (typeof KINDS)[number];

/** The level on the tenant that starting a kind needs, and so cancelling it; vacuum is an admin's. */
const NEEDS: Record<KnownKind, Level | "admin"> = {
  recompute_communities: "maintain",
  compute_metrics: "maintain",
  resolve_edges: "write",
  vacuum: "admin",
  rename_property: "owner",
};

/** A job that has not ended yet. */
export function isActiveJob(job: Job | null | undefined): boolean {
  return job?.status === "queued" || job?.status === "running";
}

/** "Graph metrics": a job kind for people; an unknown kind as it is. */
export function jobName(kind: string): string {
  return (KINDS as readonly string[]).includes(kind) ? t(`common.jobs.kinds.${kind as KnownKind}`) : kind;
}

/**
 * Whether this session may cancel the job: graphos lets whoever may start a
 * kind cancel it, and treats a kind it does not know as admin-only.
 */
export function canCancel(job: Job, session: { isAdmin: boolean; level: Level | null }): boolean {
  if (!isActiveJob(job) || job.cancel_requested) return false;
  if (session.isAdmin) return true;
  const needs = (KINDS as readonly string[]).includes(job.kind) ? NEEDS[job.kind as KnownKind] : "admin";
  return needs !== "admin" && atLeast(session.level, needs);
}

/** "45 s", "3 min", "1 h 5 min". */
export function formatDuration(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return t("common.jobs.duration.seconds", { s: seconds });
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return t("common.jobs.duration.minutes", { m: minutes });
  return t("common.jobs.duration.hours", { h: Math.floor(minutes / 60), m: minutes % 60 });
}

/** "Running for 3 min" or "Queued", and "Cancelling…" once a cancel was asked for. */
export function jobState(job: Job, now: Date = new Date()): string {
  if (job.cancel_requested) return t("common.jobs.cancelling");
  if (job.status === "queued") return t("common.jobs.queued");
  const started = new Date(job.started_at ?? job.created_at).getTime();
  return t("common.jobs.runningFor", { duration: formatDuration(now.getTime() - started) });
}

/** How a finished job ended: "finished in 12 min", "failed: …", "cancelled". */
export function jobOutcome(job: Job): string {
  if (job.status === "succeeded") {
    const started = new Date(job.started_at ?? job.created_at).getTime();
    const finished = new Date(job.finished_at ?? job.created_at).getTime();
    return t("common.jobs.succeeded", { duration: formatDuration(finished - started) });
  }
  if (job.status === "failed") return t("common.jobs.failed", { detail: job.error?.detail ?? job.status });
  return t("common.jobs.cancelled");
}

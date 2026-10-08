import type { Job } from "../src/api/types";
import { canCancel, formatDuration, isActiveJob, jobName, jobOutcome, jobState } from "../src/lib/jobs";

const job = (extra: Partial<Job> = {}): Job => ({
  id: "j",
  kind: "compute_metrics",
  tenant: "demo",
  status: "running",
  params: {},
  result: null,
  error: null,
  consumer: "sam",
  cancel_requested: false,
  sync: false,
  created_at: "2026-10-08T10:00:00Z",
  started_at: "2026-10-08T10:00:05Z",
  finished_at: null,
  ...extra,
});

const ADMIN = { isAdmin: true, canWrite: true };
const WRITER = { isAdmin: false, canWrite: true };
const READER = { isAdmin: false, canWrite: false };

describe("jobs", () => {
  it("treats queued and running jobs as active", () => {
    expect(isActiveJob(job({ status: "queued" }))).toBe(true);
    expect(isActiveJob(job())).toBe(true);
    expect(isActiveJob(job({ status: "succeeded" }))).toBe(false);
    expect(isActiveJob(null)).toBe(false);
  });

  it("names kinds for people and keeps unknown ones as they are", () => {
    expect(jobName("compute_metrics")).toBe("Graph metrics");
    expect(jobName("recompute_communities")).toBe("Group recompute");
    expect(jobName("future_kind")).toBe("future_kind");
  });

  it("lets cancel whoever may start the kind", () => {
    expect(canCancel(job(), ADMIN)).toBe(true);
    expect(canCancel(job(), WRITER)).toBe(false); // metrics: admin only
    expect(canCancel(job({ kind: "resolve_edges" }), WRITER)).toBe(true);
    expect(canCancel(job({ kind: "resolve_edges" }), READER)).toBe(false);
    expect(canCancel(job({ kind: "future_kind" }), WRITER)).toBe(false); // unknown: admin only
    expect(canCancel(job({ cancel_requested: true }), ADMIN)).toBe(false);
    expect(canCancel(job({ status: "succeeded" }), ADMIN)).toBe(false);
  });

  it("says how long and how it ended", () => {
    expect(formatDuration(42_000)).toBe("42 s");
    expect(formatDuration(3 * 60_000 + 5_000)).toBe("3 min");
    expect(formatDuration(65 * 60_000)).toBe("1 h 5 min");
    expect(jobState(job(), new Date("2026-10-08T10:03:05Z"))).toBe("Running for 3 min");
    expect(jobState(job({ status: "queued" }))).toBe("Queued");
    expect(jobState(job({ cancel_requested: true }))).toBe("Cancelling…");
    const done = job({ status: "succeeded", finished_at: "2026-10-08T10:12:05Z" });
    expect(jobOutcome(done)).toBe("finished in 12 min");
    const failed = job({ status: "failed", error: { status: 409, detail: "busy" } });
    expect(jobOutcome(failed)).toBe("failed: busy");
    expect(jobOutcome(job({ status: "cancelled" }))).toBe("cancelled");
  });
});

import type { CommunityAlgorithm, Job, ServiceInfo } from "../api/types";

/**
 * What the signed-in user can start on this server. Optional parts of graphos
 * (Louvain is a separate install) are read from `GET /v1/info`; until it has
 * answered, nothing is offered rather than a button that might fail.
 */
export type ComputeOffer = "allowed" | "notInstalled" | "noAccess" | "unknown";

/** Computing the groups needs the Louvain extra on the server, and `maintain` on the tenant. */
export function groupsOffer(canMaintain: boolean, louvain: boolean | undefined): ComputeOffer {
  if (louvain === undefined) return "unknown";
  if (!louvain) return "notInstalled";
  return canMaintain ? "allowed" : "noAccess";
}

/** The algorithms a recompute can run on this server: Louvain alone before 0.14.0, none without the extra. */
export function communityAlgorithms(features: ServiceInfo["features"] | undefined): CommunityAlgorithm[] {
  if (!features?.louvain) return [];
  return features.community_algorithms ?? ["louvain"];
}

/** How the last recompute ran, from its job: the algorithm, and whether it started from scratch. */
export function groupsRun(job: Job | null | undefined): { algorithm: CommunityAlgorithm; fromScratch: boolean } | null {
  if (!job || job.status !== "succeeded" || !job.result) return null;
  const algorithm = job.result.algorithm === "leiden" ? "leiden" : "louvain";
  return { algorithm, fromScratch: job.result.from_scratch !== false };
}

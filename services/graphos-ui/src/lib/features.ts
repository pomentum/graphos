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

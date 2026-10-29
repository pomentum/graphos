import type { Me } from "../api/types";

/** The prefix of a graphos user's or service account's subject. */
const USER = "user:";

/** The prefix of a single-sign-on subject. */
const SSO = "oidc-sub:";

/**
 * The identity provider's id of a single-sign-on manager, shortened for
 * display (its last 8 characters when long), or null for any other subject.
 */
export function ssoManagerId(subject: string | null | undefined): string | null {
  if (!subject?.startsWith(SSO)) return null;
  const id = subject.slice(SSO.length);
  return id.length > 12 ? `…${id.slice(-8)}` : id;
}

/** The name to show for a manager's subject: `user:importer` is `importer`. */
export function managerName(subject: string | null | undefined): string | null {
  if (!subject) return null;
  return subject.startsWith(USER) ? subject.slice(USER.length) : subject;
}

/**
 * Whether `me` is the schema manager `subject`: compared with the grant
 * subject graphos gives for `me` (`user:<name>`, or `oidc-sub:<sub>` for single
 * sign-on). A server that does not give it: a graphos user as `user:<consumer>`.
 */
export function isSchemaManager(
  subject: string | null | undefined,
  me: Pick<Me, "consumer" | "via" | "subject">,
): boolean {
  if (!subject) return false;
  if (me.subject !== undefined) return subject === me.subject;
  return me.via !== "oidc" && subject === `${USER}${me.consumer}`;
}

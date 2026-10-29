import type { Me } from "../api/types";

/** The prefix of a graphos user's or service account's subject. */
const USER = "user:";

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

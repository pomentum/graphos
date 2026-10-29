import type { Me } from "../api/types";

/** The prefix of a graphos user's or service account's subject. */
const USER = "user:";

/** The name to show for a manager's subject: `user:importer` is `importer`. */
export function managerName(subject: string | null | undefined): string | null {
  if (!subject) return null;
  return subject.startsWith(USER) ? subject.slice(USER.length) : subject;
}

/**
 * Whether `me` is the schema manager `subject`. Only graphos users and
 * service accounts (`user:<consumer>`) manage a schema; a person signed in
 * through single sign-on never does.
 */
export function isSchemaManager(subject: string | null | undefined, me: Pick<Me, "consumer" | "via">): boolean {
  return Boolean(subject) && me.via !== "oidc" && subject === `${USER}${me.consumer}`;
}

/**
 * Who a grant is for. A graphos user is `user:<name>`; a single sign-on
 * identity is matched against the provider's claims at sign-in:
 * `oidc-group:<group>`, `oidc-role:<role>`, `oidc-sub:<subject id>` (one
 * person) or `oidc-client:<client id>` (a program sending the provider's
 * tokens).
 */
export const SUBJECT_KINDS = ["user", "oidc-group", "oidc-role", "oidc-sub", "oidc-client"] as const;
export type SubjectKind = (typeof SUBJECT_KINDS)[number];

/** The kinds offered: single sign-on ones only where it is set up. */
export function subjectKinds(sso: boolean): SubjectKind[] {
  return sso ? [...SUBJECT_KINDS] : ["user"];
}

/** The grant subject for a name typed or picked. User names are lower case;
 *  the provider's names are kept exactly as they are, since its claims are
 *  compared as sent. */
export function subjectOf(kind: SubjectKind, name: string): string {
  const value = name.trim();
  return `${kind}:${kind === "user" ? value.toLowerCase() : value}`;
}

/** A subject's kind and name; an unknown prefix is shown whole, as a user. */
export function parseSubject(subject: string): { kind: SubjectKind; name: string } {
  const colon = subject.indexOf(":");
  const kind = subject.slice(0, colon) as SubjectKind;
  if (colon > 0 && SUBJECT_KINDS.includes(kind)) return { kind, name: subject.slice(colon + 1) };
  return { kind: "user", name: subject };
}

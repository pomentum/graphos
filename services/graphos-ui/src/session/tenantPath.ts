/**
 * The chosen tenant lives in the address, as its first segment
 * (`/demo/overview`): a reload, a new tab or a shared link lands on the same
 * tenant and page, and nothing is kept in the browser for it. The views'
 * own paths (`/overview`) are relative to it: the router's basename.
 */

/** The address of `tenant`'s views. */
export function tenantBase(tenant: string): string {
  return `/${encodeURIComponent(tenant)}`;
}

/** The tenant the first segment of `pathname` names, if it is one of `tenants`, and the path after it. */
export function splitTenantPath(pathname: string, tenants: string[]): { tenant: string | null; rest: string } {
  const match = /^\/([^/?#]+)(.*)$/.exec(pathname);
  if (!match) return { tenant: null, rest: pathname };
  const first = decodeURIComponent(match[1]!);
  if (!tenants.includes(first)) return { tenant: null, rest: pathname };
  return { tenant: first, rest: match[2] || "/" };
}

/**
 * The address to show for `tenant`, from the current one: the same page under
 * the tenant. An address of another tenant keeps only its page (its filters,
 * labels and nodes belong to that tenant); one without a tenant (an older link)
 * keeps everything.
 */
export function addressFor(tenant: string, tenants: string[], pathname: string, search: string): string {
  const { tenant: named, rest } = splitTenantPath(pathname, tenants);
  if (named === tenant) return `${pathname}${search}`;
  if (named) return `${tenantBase(tenant)}${rest === "/" ? "" : rest}`;
  return `${tenantBase(tenant)}${pathname === "/" ? "" : pathname}${search}`;
}

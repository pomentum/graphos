/**
 * Paging over a cursor API, where only "next" is known: the cursors of the
 * pages already seen are kept so "previous" can go back. The first page has
 * no cursor (`null`).
 */
export interface Pager {
  /** Cursor of each page seen so far, the first one `null`. */
  cursors: (string | null)[];
  /** The page shown, an index into `cursors`. */
  page: number;
}

export const FIRST_PAGE: Pager = { cursors: [null], page: 0 };

/** The cursor to request the page shown. */
export function currentCursor(pager: Pager): string | null {
  return pager.cursors[pager.page] ?? null;
}

/** Moves to the page after the one shown, which the server said starts at `cursor`. */
export function nextPage(pager: Pager, cursor: string): Pager {
  const cursors = [...pager.cursors.slice(0, pager.page + 1), cursor];
  return { cursors, page: pager.page + 1 };
}

/** Moves back one page; the first page stays put. */
export function previousPage(pager: Pager): Pager {
  return pager.page === 0 ? pager : { ...pager, page: pager.page - 1 };
}

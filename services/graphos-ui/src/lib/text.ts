/**
 * Shortens text to `max` characters by cutting the middle, so both the start
 * and the end stay readable: ids such as `repo/src/…/Invoice.java` differ at
 * the end, where cutting the tail would hide what tells them apart.
 */
export function middleEllipsis(text: string, max: number): string {
  if (text.length <= max) return text;
  const keep = max - 1;
  const head = Math.ceil(keep * 0.4);
  return `${text.slice(0, head)}…${text.slice(text.length - (keep - head))}`;
}

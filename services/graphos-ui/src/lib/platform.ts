/** True on macOS and iOS, where shortcuts use ⌘ rather than Ctrl. */
export function isApple(nav: Pick<Navigator, "platform" | "userAgent"> = navigator): boolean {
  return /Mac|iPhone|iPad|iPod/.test(nav.platform || nav.userAgent);
}

/** The search shortcut as this platform writes it: "⌘K" or "Ctrl K". */
export function searchShortcut(nav?: Pick<Navigator, "platform" | "userAgent">): string {
  return isApple(nav) ? "⌘K" : "Ctrl K";
}

/** True on macOS and iOS, where shortcuts use ⌘ rather than Ctrl. */
export function isApple(nav: Pick<Navigator, "platform" | "userAgent"> = navigator): boolean {
  return /Mac|iPhone|iPad|iPod/.test(nav.platform || nav.userAgent);
}

/**
 * The key that focuses search on this platform: ⌘K on Apple devices, else
 * "/". Ctrl+K is left alone on Windows and Linux, where Edge, Chrome and
 * Firefox use it to search from the address bar.
 */
export function searchShortcut(nav?: Pick<Navigator, "platform" | "userAgent">): string {
  return isApple(nav) ? "⌘K" : "/";
}

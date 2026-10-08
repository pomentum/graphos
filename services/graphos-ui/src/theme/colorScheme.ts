import type { MantineColorScheme, MantineColorSchemeManager } from "@mantine/core";

/**
 * Keeps the light/dark choice in memory for the life of the page. Mantine's
 * default manager writes it to `localStorage`; graphos stores nothing in the
 * browser, so a reload starts from the system setting again.
 */
export function memoryColorSchemeManager(): MantineColorSchemeManager {
  let current: MantineColorScheme | null = null;
  return {
    get: (defaultValue) => current ?? defaultValue,
    set: (value) => {
      current = value;
    },
    subscribe: () => undefined,
    unsubscribe: () => undefined,
    clear: () => {
      current = null;
    },
  };
}

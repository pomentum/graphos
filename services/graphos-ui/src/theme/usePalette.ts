import { useComputedColorScheme } from "@mantine/core";
import { paletteFor, type Palette } from "./tokens";

/** The palette of the colour scheme in use (light, dark, or the system's). */
export function usePalette(): Palette {
  return paletteFor(useComputedColorScheme("light"));
}

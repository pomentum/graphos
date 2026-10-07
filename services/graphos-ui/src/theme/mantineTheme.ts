import { createTheme, type CSSVariablesResolver, type MantineColorsTuple } from "@mantine/core";
import { cssVariables, DARK, LIGHT } from "./tokens";

/** Ten shades of the whitepaper blue; shade 7 is the accent itself. */
const accent: MantineColorsTuple = [
  "#EAF1FC",
  "#D4E2F9",
  "#A8C3F2",
  "#7AA3EB",
  "#5489E4",
  "#3B78E0",
  "#2A6ADB",
  "#1D5BD6",
  "#1550BE",
  "#0B44A6",
];

/** Mantine's dark scale mapped onto the graphos blacks and greys. */
const dark: MantineColorsTuple = [
  "#F2F3F5",
  "#C9CDD3",
  "#A3A9B2",
  "#8E939C",
  "#3A3D44",
  "#2A2D33",
  "#1E2024",
  "#141416",
  "#0B0B0C",
  "#060607",
];

export const theme = createTheme({
  primaryColor: "accent",
  primaryShade: 7,
  colors: { accent, dark },
  fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Text", Inter, "Segoe UI", sans-serif',
  fontFamilyMonospace: '"SF Mono", "IBM Plex Mono", ui-monospace, monospace',
  headings: { fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Display", Inter, sans-serif', fontWeight: "600" },
  fontSizes: { xs: "11px", sm: "13px", md: "14px", lg: "16px", xl: "20px" },
  radius: { xs: "4px", sm: "6px", md: "8px", lg: "10px", xl: "14px" },
  defaultRadius: "md",
  cursorType: "pointer",
});

/** Puts the graphos palette on the page as `--gx-*` variables, per colour scheme. */
export const resolveCssVariables: CSSVariablesResolver = () => ({
  variables: {},
  light: { ...cssVariables(LIGHT), "--mantine-color-body": LIGHT.window },
  dark: { ...cssVariables(DARK), "--mantine-color-body": DARK.window },
});

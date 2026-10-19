/**
 * The colour tokens of graphos UI: black and greys, with graphos's blue
 * (the website's and the diagrams') as the one accent. Every colour on screen comes from here.
 */
export interface Palette {
  /** Window frame and toolbar. */
  window: string;
  /** Sidebar and inspector. */
  panel: string;
  /** Grouped rows inside a panel. */
  group: string;
  /** Hairlines between areas. */
  separator: string;
  text: string;
  muted: string;
  /** The graph's working area. */
  canvas: string;
  /** Text fields and secondary buttons. */
  control: string;
  accent: string;
  /** The accent as text or a stroke on this background. */
  accentText: string;
  /** A quiet accent wash behind selected rows. */
  accentSoft: string;
  danger: string;
  /** Nodes and edges that are not selected. */
  nodeIdle: string;
  edgeIdle: string;
}

export const DARK: Palette = {
  window: "#0B0B0C",
  panel: "#141416",
  group: "#1E2024",
  separator: "rgba(255, 255, 255, 0.08)",
  text: "#F2F3F5",
  muted: "#8E939C",
  canvas: "#26292E",
  control: "#2A2D33",
  accent: "#1D5BD6",
  accentText: "#8AB0FF",
  accentSoft: "#1A2640",
  danger: "#F2877F",
  nodeIdle: "#5A5F68",
  edgeIdle: "#3D4148",
};

export const LIGHT: Palette = {
  window: "#FBFBFD",
  panel: "#F0F0F3",
  group: "#FFFFFF",
  separator: "rgba(0, 0, 0, 0.09)",
  text: "#1D1D1F",
  muted: "#6E6E73",
  canvas: "#E3E5E9",
  control: "#E8E8ED",
  accent: "#1D5BD6",
  accentText: "#1D5BD6",
  accentSoft: "#DDE7FA",
  danger: "#B3261E",
  nodeIdle: "#8A9099",
  edgeIdle: "#B9BEC6",
};

export function paletteFor(scheme: "light" | "dark"): Palette {
  return scheme === "dark" ? DARK : LIGHT;
}

/** The palette as `--gx-*` CSS variables, for stylesheets. */
export function cssVariables(palette: Palette): Record<string, string> {
  return Object.fromEntries(
    Object.entries(palette).map(([name, value]) => [
      `--gx-${name.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`,
      value,
    ]),
  );
}

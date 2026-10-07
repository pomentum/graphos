/**
 * Muted category colours for "colour by label / group": readable on both the
 * light and the dark canvas, and quieter than the accent blue, which stays
 * reserved for the selection.
 */
export const CATEGORY_COLORS = [
  "#C9825B", // clay
  "#5FA584", // sage
  "#A98AD0", // lavender
  "#C7AE55", // ochre
  "#5BA9BF", // teal
  "#C96F8E", // rose
  "#8DA85A", // olive
  "#8D97AE", // slate
  "#B98D63", // sand
  "#6F8FC2", // steel
] as const;

/**
 * Colours for categories ranked most common first: each of the first ten gets
 * its own colour, so no two in the legend look alike; the rest get none.
 */
export function rankColors(ranked: readonly string[]): Map<string, string> {
  return new Map(ranked.slice(0, CATEGORY_COLORS.length).map((value, i) => [value, CATEGORY_COLORS[i]!]));
}

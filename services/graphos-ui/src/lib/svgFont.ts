import cyrillicExt from "@fontsource/inter/files/inter-cyrillic-ext-500-normal.woff2?url";
import cyrillic from "@fontsource/inter/files/inter-cyrillic-500-normal.woff2?url";
import greekExt from "@fontsource/inter/files/inter-greek-ext-500-normal.woff2?url";
import greek from "@fontsource/inter/files/inter-greek-500-normal.woff2?url";
import latinExt from "@fontsource/inter/files/inter-latin-ext-500-normal.woff2?url";
import latin from "@fontsource/inter/files/inter-latin-500-normal.woff2?url";
import vietnamese from "@fontsource/inter/files/inter-vietnamese-500-normal.woff2?url";

/**
 * Inter at the canvas labels' weight (500), as `@font-face` rules for an SVG
 * file: only the subsets its text needs, each embedded as data, so the file
 * shows the same letters on a machine without Inter. The subsets and their
 * ranges are those of `@fontsource/inter/500.css`.
 */

export const SVG_FONT_FAMILY = "Inter, -apple-system, BlinkMacSystemFont, sans-serif";

interface Subset {
  url: string;
  range: string;
}

const SUBSETS: Subset[] = [
  { url: cyrillicExt, range: "U+0460-052F,U+1C80-1C8A,U+20B4,U+2DE0-2DFF,U+A640-A69F,U+FE2E-FE2F" },
  { url: cyrillic, range: "U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116" },
  { url: greekExt, range: "U+1F00-1FFF" },
  { url: greek, range: "U+0370-0377,U+037A-037F,U+0384-038A,U+038C,U+038E-03A1,U+03A3-03FF" },
  {
    url: vietnamese,
    range:
      "U+0102-0103,U+0110-0111,U+0128-0129,U+0168-0169,U+01A0-01A1,U+01AF-01B0,U+0300-0301," +
      "U+0303-0304,U+0308-0309,U+0323,U+0329,U+1EA0-1EF9,U+20AB",
  },
  {
    url: latinExt,
    range:
      "U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329," +
      "U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF",
  },
  {
    url: latin,
    range:
      "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329," +
      "U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD",
  },
];

function covers(range: string, code: number): boolean {
  return range.split(",").some((part) => {
    const [from, to] = part.slice(2).split("-");
    const low = parseInt(from!, 16);
    return code >= low && code <= (to ? parseInt(to, 16) : low);
  });
}

/** The subsets with a letter of `text` (none for no text). */
export function subsetsFor(text: string): Subset[] {
  const codes = new Set([...text].map((c) => c.codePointAt(0)!));
  return SUBSETS.filter((subset) => [...codes].some((code) => covers(subset.range, code)));
}

async function asDataUrl(url: string): Promise<string> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: ${response.status}`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return `data:font/woff2;base64,${btoa(binary)}`;
}

/** `@font-face` rules for the letters of `text`, the font embedded. */
export async function fontFaces(text: string): Promise<string> {
  const faces = await Promise.all(
    subsetsFor(text).map(
      async (subset) =>
        `@font-face{font-family:"Inter";font-style:normal;font-weight:500;` +
        `src:url(${await asDataUrl(subset.url)}) format("woff2");unicode-range:${subset.range};}`,
    ),
  );
  return faces.join("");
}

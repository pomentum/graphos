import en from "../src/i18n/en.json";

/** Every source file of the app, as text. */
const sources = import.meta.glob<string>("../src/**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true });

/** Plural forms i18next picks from with `count`. */
const PLURAL_SUFFIXES = ["_zero", "_one", "_two", "_few", "_many", "_other"];

/** The leaves of the JSON, as dotted keys with their text. */
function leaves(node: unknown, prefix = ""): [string, unknown][] {
  if (node === null || typeof node !== "object") return [[prefix, node]];
  return Object.entries(node).flatMap(([key, value]) => leaves(value, prefix ? `${prefix}.${key}` : key));
}

const texts = new Map(leaves(en));

/** Whether `key` resolves to a text, directly or as the base of plural forms. */
function exists(key: string): boolean {
  return texts.has(key) || PLURAL_SUFFIXES.some((suffix) => texts.has(`${key}${suffix}`));
}

/** `[file, key]` for each literal key used in the source; template-literal keys are left out. */
function usedKeys(pattern: RegExp): [string, string][] {
  return Object.entries(sources).flatMap(([file, text]) =>
    [...text.matchAll(pattern)].map((match) => [file, match[1]!] as [string, string]),
  );
}

describe("i18n", () => {
  it("finds the app's sources", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(20);
  });

  it("has a text in en.json for every key passed to t()", () => {
    const used = usedKeys(/\bt\(\s*"([^"]+)"/g);
    expect(used.length).toBeGreaterThan(100);
    expect(used.filter(([, key]) => !exists(key))).toEqual([]);
  });

  it("has a text for every key stored as data (mutation meta, view titles, filter labels)", () => {
    const stored = usedKeys(/"((?:common|shell|views|components|notices|errors)\.[A-Za-z0-9_.]+)"/g);
    expect(stored.filter(([, key]) => !exists(key))).toEqual([]);
  });

  it("has no empty texts", () => {
    expect([...texts].filter(([, text]) => typeof text !== "string" || text.trim() === "")).toEqual([]);
  });

  it("has both plural forms wherever it has one", () => {
    const keys = [...texts.keys()];
    const unpaired = keys.filter(
      (key) =>
        (key.endsWith("_one") && !texts.has(key.replace(/_one$/, "_other"))) ||
        (key.endsWith("_other") && !texts.has(key.replace(/_other$/, "_one"))),
    );
    expect(unpaired).toEqual([]);
  });
});

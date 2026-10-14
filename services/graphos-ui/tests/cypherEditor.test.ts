import { CompletionContext, type CompletionResult } from "@codemirror/autocomplete";
import { StringStream } from "@codemirror/language";
import { EditorState } from "@codemirror/state";
import {
  boundVariables,
  cypher,
  cypherCompletions,
  cypherToken,
  openBracket,
  type CypherVocabulary,
} from "../src/views/query/cypherLanguage";

/** Each token of one line of Cypher with what it is coloured as. */
function tokens(line: string): [string, string][] {
  const state = { comment: false, brackets: [] as string[], label: false, property: false };
  const stream = new StringStream(line, 2, 2);
  const out: [string, string][] = [];
  while (!stream.eol()) {
    stream.start = stream.pos;
    const style = cypherToken(stream, state);
    if (style && style !== "punctuation") out.push([stream.current(), style]);
  }
  return out;
}

const VOCABULARY: CypherVocabulary = {
  vertexLabels: ["Author", "Doc"],
  edgeTypes: ["WROTE", "CITES"],
  properties: { name: ["Author"], title: ["Doc"], pages: ["Doc"] },
  params: ["author"],
};

/** The suggestions with the cursor at `¦`. */
function suggest(text: string, explicit = false): { from: number; labels: string[] } | null {
  const pos = text.indexOf("¦");
  const doc = text.replace("¦", "");
  const state = EditorState.create({ doc, extensions: [cypher] });
  const result = cypherCompletions(() => VOCABULARY)(
    new CompletionContext(state, pos, explicit),
  ) as CompletionResult | null;
  if (!result) return null;
  const sorted = [...result.options].sort((a, b) => (b.boost ?? 0) - (a.boost ?? 0));
  return { from: result.from, labels: sorted.map((o) => o.label) };
}

describe("Cypher colours", () => {
  it("tells labels, edge types, properties, parameters, functions and keywords apart", () => {
    expect(tokens("MATCH (a:Author {name: $who})-[w:WROTE|CITES]->(d) WHERE d.pages > 3 RETURN count(d)")).toEqual([
      ["MATCH", "keyword"],
      ["a", "variable"],
      ["Author", "nodeLabel"],
      ["name", "property"],
      ["$who", "param"],
      ["-", "operator"],
      ["w", "variable"],
      ["WROTE", "edgeType"],
      ["|", "operator"],
      ["CITES", "edgeType"],
      ["-", "operator"],
      [">", "operator"],
      ["d", "variable"],
      ["WHERE", "keyword"],
      ["d", "variable"],
      ["pages", "property"],
      [">", "operator"],
      ["3", "number"],
      ["RETURN", "keyword"],
      ["count", "function"],
      ["d", "variable"],
    ]);
  });

  it("colours strings and comments whole, and a map's values not as labels", () => {
    expect(tokens("RETURN 'it''s (a:B)' // MATCH (n:X)")).toEqual([
      ["RETURN", "keyword"],
      ["'it'", "string"],
      ["'s (a:B)'", "string"],
      ["// MATCH (n:X)", "comment"],
    ]);
    expect(tokens("MATCH (n {kind: other}) RETURN true")).toContainEqual(["other", "variable"]);
    expect(tokens("MATCH (n {kind: other}) RETURN true")).toContainEqual(["true", "atom"]);
  });
});

describe("Cypher suggestions", () => {
  it("offers vertex labels in a node and edge types in an edge", () => {
    expect(suggest("MATCH (a:¦")?.labels).toEqual(["Author", "Doc"]);
    expect(suggest("MATCH (a:Au¦")).toMatchObject({ from: 9 });
    expect(suggest("MATCH (a)-[:¦")?.labels).toEqual(["WROTE", "CITES"]);
    expect(suggest("MATCH (a)-[r:WROTE|¦")?.labels).toEqual(["WROTE", "CITES"]);
  });

  it("offers properties after a variable, its label's first", () => {
    const result = suggest("MATCH (d:Doc) RETURN d.¦");
    expect(result?.labels.slice(0, 2).sort()).toEqual(["pages", "title"]);
    expect(result?.labels).toContain("name");
    expect(suggest("MATCH (a:Author {n¦")?.labels).toContain("name");
  });

  it("offers the query's parameters and the saved query's", () => {
    expect(suggest("MATCH (a {id: $x}) WHERE a.n = $¦")?.labels.sort()).toEqual(["author", "x"]);
  });

  it("offers clauses, functions and the query's variables, and whole queries at the start", () => {
    const words = suggest("MATCH (who:Author) RETURN w¦")!.labels;
    expect(words[0]).toBe("who");
    expect(words).toContain("WHERE");
    expect(words).toContain("count");
    expect(words).not.toContain("MATCH … RETURN");
    expect(suggest("¦", true)!.labels).toContain("MATCH … RETURN");
  });

  it("stays quiet inside strings and comments", () => {
    expect(suggest("RETURN 'a:¦")).toBeNull();
    expect(suggest("// MATCH (a:¦")).toBeNull();
  });

  it("knows the open bracket and the bound variables", () => {
    expect(openBracket("MATCH (a {x: '(['})-[")).toBe("[");
    expect(openBracket("RETURN [1, 2]")).toBeNull();
    expect([...boundVariables("MATCH (a:Author)-[w:WROTE]->(d) RETURN d.title AS t")]).toEqual([
      ["a", "Author"],
      ["w", null],
      ["d", null],
      ["t", null],
    ]);
  });
});

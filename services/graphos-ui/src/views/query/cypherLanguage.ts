import {
  snippetCompletion,
  type Completion,
  type CompletionContext,
  type CompletionResult,
} from "@codemirror/autocomplete";
import { HighlightStyle, StreamLanguage, type StringStream } from "@codemirror/language";
import { tags } from "@lezer/highlight";

/**
 * Cypher in the query editor: colours for its words, and suggestions drawn
 * from the tenant (its labels, edge types and property names) and from the
 * query itself (its variables and parameters).
 */

/** Clauses and operators, as suggested (the console only reads: no write clauses). */
export const KEYWORDS = [
  "MATCH",
  "OPTIONAL MATCH",
  "WHERE",
  "RETURN",
  "WITH",
  "UNWIND",
  "ORDER BY",
  "SKIP",
  "LIMIT",
  "DISTINCT",
  "AS",
  "AND",
  "OR",
  "XOR",
  "NOT",
  "IN",
  "IS NULL",
  "IS NOT NULL",
  "STARTS WITH",
  "ENDS WITH",
  "CONTAINS",
  "CASE",
  "WHEN",
  "THEN",
  "ELSE",
  "END",
  "UNION",
  "UNION ALL",
  "ASC",
  "DESC",
  "EXISTS",
];

/** Every keyword the colours know, write clauses included (they are refused, but still Cypher). */
const KEYWORD_WORDS = new Set(
  [
    ...KEYWORDS.flatMap((k) => k.split(" ")),
    "BY",
    "CREATE",
    "MERGE",
    "SET",
    "DELETE",
    "DETACH",
    "REMOVE",
    "CALL",
    "YIELD",
    "ON",
    "ALL",
    "ANY",
    "NONE",
    "SINGLE",
    "ASCENDING",
    "DESCENDING",
  ].map((k) => k.toLowerCase()),
);

const ATOMS = new Set(["true", "false", "null"]);

/** Functions AGE's Cypher knows, each with a one-line hint. */
export const FUNCTIONS: [name: string, hint: string][] = [
  ["count", "number of rows, or of non-null values"],
  ["collect", "values gathered into a list"],
  ["sum", "sum of the values"],
  ["avg", "average of the values"],
  ["min", "smallest value"],
  ["max", "largest value"],
  ["id", "a node's or edge's graph id"],
  ["label", "a node's label, or an edge's type"],
  ["type", "an edge's type"],
  ["properties", "the properties as a map"],
  ["keys", "the property names"],
  ["start_id", "an edge's source id"],
  ["end_id", "an edge's target id"],
  ["startNode", "an edge's source node"],
  ["endNode", "an edge's target node"],
  ["nodes", "the nodes of a path"],
  ["relationships", "the edges of a path"],
  ["length", "a path's length"],
  ["size", "a list's or a string's length"],
  ["head", "a list's first item"],
  ["last", "a list's last item"],
  ["tail", "a list without its first item"],
  ["range", "a list of integers"],
  ["reverse", "a list or string reversed"],
  ["coalesce", "the first value that is not null"],
  ["exists", "whether a property is set"],
  ["toString", "as a string"],
  ["toInteger", "as an integer"],
  ["toFloat", "as a float"],
  ["toBoolean", "as a boolean"],
  ["toLower", "lower case"],
  ["toUpper", "upper case"],
  ["trim", "without surrounding spaces"],
  ["ltrim", "without leading spaces"],
  ["rtrim", "without trailing spaces"],
  ["replace", "every match replaced"],
  ["substring", "part of a string"],
  ["left", "the first characters"],
  ["right", "the last characters"],
  ["split", "a string cut into a list"],
  ["abs", "absolute value"],
  ["ceil", "rounded up"],
  ["floor", "rounded down"],
  ["round", "rounded"],
  ["sqrt", "square root"],
  ["rand", "a random number in [0, 1)"],
  ["percentileCont", "a percentile, interpolated"],
  ["percentileDisc", "a percentile, of the values"],
  ["stDev", "standard deviation of a sample"],
  ["timestamp", "milliseconds since 1970"],
];

const FUNCTION_NAMES = new Set(FUNCTIONS.map(([name]) => name.toLowerCase()));

interface TokenState {
  /** Inside a block comment. */
  comment: boolean;
  /** Open brackets, innermost last: `(` a node, `[` an edge or a list, `{` a map. */
  brackets: string[];
  /** The next name is a label (after `:` or `|` in a pattern). */
  label: boolean;
  /** The next name is a property (after `.`). */
  property: boolean;
}

function readName(stream: StringStream): boolean {
  if (stream.eat("`")) {
    stream.skipTo("`") ? stream.next() : stream.skipToEnd();
    return true;
  }
  return Boolean(stream.match(/^[A-Za-z_][A-Za-z0-9_]*/));
}

/** Reads one token of Cypher (exported for tests). */
export function cypherToken(stream: StringStream, state: TokenState): string | null {
  if (state.comment) {
    if (stream.skipTo("*/")) {
      stream.match("*/");
      state.comment = false;
    } else stream.skipToEnd();
    return "comment";
  }
  if (stream.eatSpace()) return null;
  if (stream.match("//")) {
    stream.skipToEnd();
    return "comment";
  }
  if (stream.match("/*")) {
    state.comment = true;
    return cypherToken(stream, state) ?? "comment";
  }
  const quote = stream.peek();
  if (quote === "'" || quote === '"') {
    stream.next();
    let escaped = false;
    for (let ch = stream.next(); ch !== undefined; ch = stream.next()) {
      if (ch === quote && !escaped) break;
      escaped = !escaped && ch === "\\";
    }
    return "string";
  }
  if (stream.match(/^\$[A-Za-z_][A-Za-z0-9_]*/)) return "param";
  if (stream.match(/^\d+(\.\d+)?([eE][-+]?\d+)?/)) return "number";
  const top = state.brackets[state.brackets.length - 1];
  const start = stream.pos;
  if (readName(stream)) {
    const word = stream.string.slice(start, stream.pos).toLowerCase();
    if (state.label) {
      state.label = false;
      return top === "[" ? "edgeType" : "nodeLabel";
    }
    if (state.property) {
      state.property = false;
      return "property";
    }
    // A key of a map: `{name: …}`.
    if (top === "{" && stream.match(/^\s*:/, false)) return "property";
    if (stream.match(/^\s*\(/, false) && FUNCTION_NAMES.has(word)) return "function";
    if (ATOMS.has(word)) return "atom";
    if (KEYWORD_WORDS.has(word)) return "keyword";
    return "variable";
  }
  const ch = stream.next() ?? "";
  if (ch === "(" || ch === "[" || ch === "{") state.brackets.push(ch);
  else if (ch === ")" || ch === "]" || ch === "}") state.brackets.pop();
  else if ((ch === ":" || ch === "|") && (top === "(" || top === "[")) state.label = true;
  else if (ch === "." && !stream.match(".", false) && stream.match(/^[A-Za-z_`]/, false)) state.property = true;
  return /[-<>=+*/%^!|]/.test(ch) ? "operator" : "punctuation";
}

export const cypher = StreamLanguage.define<TokenState>({
  name: "cypher",
  startState: () => ({ comment: false, brackets: [], label: false, property: false }),
  copyState: (s) => ({ ...s, brackets: [...s.brackets] }),
  token: cypherToken,
  tokenTable: {
    nodeLabel: tags.typeName,
    edgeType: tags.labelName,
    param: tags.special(tags.variableName),
    function: tags.function(tags.variableName),
    property: tags.propertyName,
    variable: tags.variableName,
  },
  languageData: { commentTokens: { line: "//", block: { open: "/*", close: "*/" } } },
});

/**
 * The colours: the canvas's category colours, quiet enough for both themes;
 * keywords in the accent, as the rest of the UI marks what acts.
 */
export const cypherHighlight = HighlightStyle.define([
  { tag: tags.keyword, color: "var(--gx-accent-text)", fontWeight: "600" },
  { tag: tags.typeName, color: "#5FA584" },
  { tag: tags.labelName, color: "#C9825B" },
  { tag: tags.special(tags.variableName), color: "#C96F8E" },
  { tag: tags.function(tags.variableName), color: "#5BA9BF" },
  { tag: tags.propertyName, color: "#A98AD0" },
  { tag: [tags.string, tags.number, tags.atom], color: "#C7AE55" },
  { tag: tags.comment, color: "var(--gx-muted)", fontStyle: "italic" },
  { tag: tags.operator, color: "var(--gx-muted)" },
]);

/** What the editor suggests from: the tenant's schema and properties, the saved query's parameters. */
export interface CypherVocabulary {
  vertexLabels: string[];
  edgeTypes: string[];
  /** Property name → the labels whose nodes carry it. */
  properties: Record<string, string[]>;
  params?: string[];
}

/** The innermost open bracket before the end of `text` (strings and comments skipped). */
export function openBracket(text: string): string | null {
  const stack: string[] = [];
  const code = text
    .replace(/\/\*[\s\S]*?(\*\/|$)/g, " ")
    .replace(/\/\/[^\n]*/g, " ")
    .replace(/'(?:[^'\\]|\\.)*'?|"(?:[^"\\]|\\.)*"?|`[^`]*`?/g, "x");
  for (const ch of code) {
    if (ch === "(" || ch === "[" || ch === "{") stack.push(ch);
    else if (ch === ")" || ch === "]" || ch === "}") stack.pop();
  }
  return stack[stack.length - 1] ?? null;
}

/** The variables a query binds in its patterns and with `AS`, with the label each node variable was given. */
export function boundVariables(query: string): Map<string, string | null> {
  const found = new Map<string, string | null>();
  for (const m of query.matchAll(/[([]\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*`?([A-Za-z_][A-Za-z0-9_]*))?/g)) {
    const name = m[1]!;
    if (KEYWORD_WORDS.has(name.toLowerCase())) continue;
    if (!found.get(name)) found.set(name, m[0].startsWith("(") ? (m[2] ?? null) : null);
  }
  for (const m of query.matchAll(/\b(?:AS|as|As)\s+([A-Za-z_][A-Za-z0-9_]*)/g)) {
    if (!found.has(m[1]!)) found.set(m[1]!, null);
  }
  return found;
}

const SNIPPETS: Completion[] = [
  snippetCompletion("MATCH (${n}:${Label}) RETURN ${n} LIMIT 25", {
    label: "MATCH … RETURN",
    detail: "nodes of a label",
    type: "text",
  }),
  snippetCompletion("MATCH (${a})-[${r}:${TYPE}]->(${b}) RETURN ${a}, ${r}, ${b} LIMIT 25", {
    label: "MATCH (a)-[r]->(b)",
    detail: "edges and their ends",
    type: "text",
  }),
  snippetCompletion("MATCH p = (${a} {id: ${$from}})-[*1..3]-(${b} {id: ${$to}}) RETURN p LIMIT 5", {
    label: "MATCH p = path",
    detail: "paths between two nodes",
    type: "text",
  }),
  snippetCompletion("MATCH (${n}:${Label}) RETURN ${n}.${property} AS ${value}, count(*) AS n ORDER BY n DESC", {
    label: "count by value",
    detail: "how often each value occurs",
    type: "text",
  }),
];

/**
 * Suggestions at the cursor: labels after `:` in a node, edge types after
 * `:` or `|` in an edge, properties after `variable.` (those of the
 * variable's label first), parameters after `$`, else clauses, functions,
 * the query's variables and a few whole queries.
 */
export function cypherCompletions(vocabulary: () => CypherVocabulary) {
  return (context: CompletionContext): CompletionResult | null => {
    const before = context.state.sliceDoc(0, context.pos);
    const inside = openBracket(before);
    // Inside a string or a comment: nothing to suggest.
    if (/'(?:[^'\\\n]|\\.)*$|"(?:[^"\\\n]|\\.)*$|\/\/[^\n]*$/.test(before.split("\n").pop() ?? "")) return null;
    const words = vocabulary();

    const label = context.matchBefore(/[:|]\s*`?[A-Za-z0-9_]*$/);
    if (label && (inside === "(" || inside === "[")) {
      const from = label.from + label.text.search(/[A-Za-z0-9_`]|$/);
      if (label.text.startsWith("|") && inside !== "[") return null;
      const names = inside === "[" ? words.edgeTypes : words.vertexLabels;
      return {
        from,
        options: names.map((name) => ({ label: name, type: inside === "[" ? "type" : "class" })),
        validFor: /^`?[A-Za-z0-9_]*$/,
      };
    }

    const property = context.matchBefore(/([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z0-9_]*$/);
    if (property) {
      const variable = property.text.slice(0, property.text.indexOf("."));
      const ofLabel = boundVariables(context.state.doc.toString()).get(variable) ?? null;
      const options = Object.entries(words.properties).map(([name, labels]) => {
        const own = ofLabel !== null && labels.includes(ofLabel);
        return {
          label: name,
          type: "property",
          detail: own ? ofLabel! : labels.slice(0, 2).join(", ") + (labels.length > 2 ? ", …" : ""),
          boost: own ? 10 : 0,
        };
      });
      return { from: property.from + variable.length + 1, options, validFor: /^[A-Za-z0-9_]*$/ };
    }

    const param = context.matchBefore(/\$[A-Za-z0-9_]*$/);
    if (param) {
      const used = new Set([...context.state.doc.toString().matchAll(/\$([A-Za-z_][A-Za-z0-9_]*)/g)].map((m) => m[1]!));
      for (const p of words.params ?? []) used.add(p);
      const typed = param.text.slice(1);
      if (typed) used.delete(typed);
      return {
        from: param.from + 1,
        options: [...used].map((name) => ({ label: name, type: "variable" })),
        validFor: /^[A-Za-z0-9_]*$/,
      };
    }

    const word = context.matchBefore(/[A-Za-z_][A-Za-z0-9_]*$/);
    if (!word && !context.explicit) return null;
    const from = word?.from ?? context.pos;
    // A key of a map: the property names.
    if (inside === "{" && /[{,]\s*$/.test(before.slice(0, from))) {
      return {
        from,
        options: Object.keys(words.properties).map((name) => ({ label: name, type: "property" })),
        validFor: /^[A-Za-z0-9_]*$/,
      };
    }
    const variables = [...boundVariables(context.state.doc.toString()).keys()].filter((v) => v !== word?.text);
    const options: Completion[] = [
      ...KEYWORDS.map((k) => ({ label: k, type: "keyword", boost: -1 })),
      ...FUNCTIONS.map(([name, hint]) => ({
        label: name,
        type: "function",
        detail: hint,
        apply: `${name}(`,
        boost: -2,
      })),
      ...variables.map((v) => ({ label: v, type: "variable", boost: 2 })),
      ...(from === 0 || /^\s*$/.test(before.slice(0, from)) ? SNIPPETS : []),
    ];
    return { from, options, validFor: /^[A-Za-z0-9_]*$/ };
  };
}

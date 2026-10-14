import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { bracketMatching, syntaxHighlighting } from "@codemirror/language";
import { EditorState, Prec } from "@codemirror/state";
import { EditorView, keymap, placeholder } from "@codemirror/view";
import { useEffect, useRef } from "react";
import { cypher, cypherCompletions, cypherHighlight, type CypherVocabulary } from "./cypherLanguage";
import classes from "./QueryView.module.css";

const theme = EditorView.theme({
  "&": { fontSize: "13px", maxHeight: "360px" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "var(--mantine-font-family-monospace)", lineHeight: "1.55", overflow: "auto" },
  ".cm-content": { minHeight: "110px", padding: "8px 0", caretColor: "var(--gx-text)" },
  ".cm-line": { padding: "0 12px" },
  ".cm-placeholder": { color: "var(--gx-muted)" },
  ".cm-matchingBracket": { background: "var(--gx-accent-soft)", outline: "none" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": {
    background: "var(--gx-accent-soft) !important",
  },
  ".cm-tooltip": {
    background: "var(--gx-panel)",
    border: "0.5px solid var(--gx-separator)",
    borderRadius: "8px",
    overflow: "hidden",
  },
  ".cm-tooltip-autocomplete > ul": { fontFamily: "var(--mantine-font-family-monospace)", fontSize: "12px" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": { background: "var(--gx-accent)", color: "#fff" },
  ".cm-completionDetail": { color: "var(--gx-muted)", fontStyle: "normal", marginLeft: "12px" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected] .cm-completionDetail": { color: "inherit" },
});

/**
 * The Cypher editor: coloured as Cypher, with suggestions (labels, edge
 * types, properties, parameters, clauses and functions) as one types, or on
 * Ctrl+Space; Ctrl/⌘+Enter runs.
 */
export function CypherEditor({
  value,
  onChange,
  onRun,
  vocabulary,
  label,
  hint,
}: {
  value: string;
  onChange: (value: string) => void;
  onRun: () => void;
  vocabulary: CypherVocabulary;
  label: string;
  hint: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  // Read when used, so the editor is made once and always sees the latest.
  const latest = useRef({ onChange, onRun, vocabulary });
  latest.current = { onChange, onRun, vocabulary };

  useEffect(() => {
    const editor = new EditorView({
      parent: host.current!,
      state: EditorState.create({
        doc: value,
        extensions: [
          Prec.highest(
            keymap.of([
              {
                key: "Mod-Enter",
                run: () => {
                  latest.current.onRun();
                  return true;
                },
              },
            ]),
          ),
          history(),
          closeBrackets(),
          bracketMatching(),
          cypher,
          syntaxHighlighting(cypherHighlight),
          autocompletion({ override: [cypherCompletions(() => latest.current.vocabulary)], icons: false }),
          keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...historyKeymap, ...completionKeymap]),
          placeholder(hint),
          EditorView.lineWrapping,
          EditorView.contentAttributes.of({ "aria-label": label, spellcheck: "false" }),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) latest.current.onChange(update.state.doc.toString());
          }),
          theme,
        ],
      }),
    });
    view.current = editor;
    return () => {
      editor.destroy();
      view.current = null;
    };
    // Made once; later values arrive through the effect below.
  }, []);

  // A saved query chosen beside: the editor shows it.
  useEffect(() => {
    const editor = view.current;
    if (editor && editor.state.doc.toString() !== value) {
      editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: value } });
    }
  }, [value]);

  return <div ref={host} className={classes.editor} />;
}

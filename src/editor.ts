import { EditorState, type Text } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
// Only these imports, not `markdown()`: that one bundles HTML, CSS and JavaScript highlighting.
import { markdownKeymap, markdownLanguage } from "@codemirror/lang-markdown";
import { tags } from "@lezer/highlight";

// Until inline rendering exists: markup is dimmed, emphasis and headings look like what they mean.
const highlight = HighlightStyle.define([
  { tag: tags.heading, fontWeight: "700" },
  { tag: tags.strong, fontWeight: "700" },
  { tag: tags.emphasis, fontStyle: "italic" },
  { tag: tags.strikethrough, textDecoration: "line-through" },
  { tag: [tags.link, tags.url], color: "var(--accent)" },
  { tag: tags.monospace, fontFamily: "var(--mono)", fontSize: "0.9em" },
  { tag: [tags.processingInstruction, tags.contentSeparator, tags.quote], color: "var(--muted)" },
]);

const theme = EditorView.theme({
  "&": { height: "100%", fontSize: "16px" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "inherit", lineHeight: "1.6" },
  ".cm-content": { maxWidth: "72ch", margin: "0 auto", padding: "40px 32px", caretColor: "var(--fg)" },
});

// A fresh editor state for `text`, with its own undo history.
export function createState(text: string, onChange: (doc: Text) => void): EditorState {
  return EditorState.create({
    doc: text,
    extensions: [
      history(),
      keymap.of([...markdownKeymap, ...defaultKeymap, ...historyKeymap, indentWithTab]),
      markdownLanguage,
      syntaxHighlighting(highlight),
      theme,
      EditorView.lineWrapping,
      EditorView.contentAttributes.of({ spellcheck: "false", autocorrect: "off", autocapitalize: "off" }),
      EditorView.updateListener.of((update) => {
        if (update.docChanged) onChange(update.state.doc);
      }),
    ],
  });
}

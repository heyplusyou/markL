import { Compartment, EditorState, type Text } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
// Only these imports, not `markdown()`: that one bundles HTML, CSS and JavaScript highlighting.
import { markdownKeymap } from "@codemirror/lang-markdown";
import { tags } from "@lezer/highlight";
import { inlinePreview } from "./preview";
import { codeLanguageSupport, previewMarkdown } from "./code-languages";
import { imageSupport } from "./images";
import { tablePreview } from "./tables";

const editing = new Compartment();

export function setEditingLocked(view: EditorView, locked: boolean) {
  view.dispatch({ effects: editing.reconfigure([
    EditorState.readOnly.of(locked), EditorView.editable.of(!locked),
  ]) });
}

// Source highlighting remains visible on the lines being edited and in literal blocks.
const highlight = HighlightStyle.define([
  { tag: tags.heading, fontWeight: "700" },
  { tag: tags.strong, fontWeight: "700" },
  { tag: tags.emphasis, fontStyle: "italic" },
  { tag: tags.strikethrough, textDecoration: "line-through" },
  { tag: [tags.link, tags.url], color: "var(--accent)" },
  { tag: tags.monospace, fontFamily: "var(--mono)", fontSize: "0.9em" },
  { tag: [tags.processingInstruction, tags.contentSeparator], color: "var(--muted)" },
  { tag: tags.keyword, class: "cm-code-keyword" },
  { tag: [tags.string, tags.regexp, tags.attributeValue], class: "cm-code-string" },
  { tag: [tags.number, tags.bool, tags.null], class: "cm-code-number" },
  { tag: [tags.typeName, tags.className, tags.tagName], class: "cm-code-type" },
  { tag: [tags.propertyName, tags.attributeName], class: "cm-code-property" },
  { tag: tags.comment, class: "cm-code-comment" },
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
      editing.of([]),
      keymap.of([...markdownKeymap, ...defaultKeymap, ...historyKeymap, indentWithTab]),
      previewMarkdown,
      codeLanguageSupport(),
      syntaxHighlighting(highlight),
      imageSupport(),
      tablePreview,
      inlinePreview,
      theme,
      EditorView.lineWrapping,
      EditorView.contentAttributes.of({ spellcheck: "false", autocorrect: "off", autocapitalize: "off" }),
      EditorView.updateListener.of((update) => {
        if (update.docChanged) onChange(update.state.doc);
      }),
    ],
  });
}

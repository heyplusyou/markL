import { Language, LanguageSupport, ParseContext } from "@codemirror/language";
import { markdownLanguage } from "@codemirror/lang-markdown";
import { type MarkdownParser, parseCode } from "@lezer/markdown";
import { Facet, type EditorState } from "@codemirror/state";

// The markdown parser may encounter an offscreen fence. It gets a cheap skipping
// parser, not a network request. Only a visible block opens its language's gate.
function language(load: () => Promise<LanguageSupport>) {
  return { load, support: undefined as LanguageSupport | undefined,
    promise: undefined as Promise<void> | undefined, failed: false };
}
type Entry = ReturnType<typeof language>;
type Gate = { ready: Promise<void>; resolve: () => void };
const sessions = Facet.define<Map<Entry, Gate>, Map<Entry, Gate>>({ combine: (values) => values[0] });
export const codeLanguageSupport = () => sessions.of(new Map());

function gate(state: EditorState | undefined, entry: Entry) {
  if (!state) return undefined;
  const session = state.facet(sessions);
  if (!session.has(entry)) {
    let resolve!: () => void;
    const ready = new Promise<void>((done) => { resolve = done; });
    session.set(entry, { ready, resolve });
  }
  return session.get(entry)!;
}

const javascript = language(() => import("@codemirror/lang-javascript").then((m) => new LanguageSupport(m.javascriptLanguage)));
const typescript = language(() => import("@codemirror/lang-javascript").then((m) => new LanguageSupport(m.typescriptLanguage)));
const jsx = language(() => import("@codemirror/lang-javascript").then((m) => new LanguageSupport(m.jsxLanguage)));
const tsx = language(() => import("@codemirror/lang-javascript").then((m) => new LanguageSupport(m.tsxLanguage)));
const json = language(() => import("@codemirror/lang-json").then((m) => new LanguageSupport(m.jsonLanguage)));
const css = language(() => import("@codemirror/lang-css").then((m) => new LanguageSupport(m.cssLanguage)));
const html = language(() => import("@codemirror/lang-html").then((m) => new LanguageSupport(m.htmlLanguage)));
const python = language(() => import("@codemirror/lang-python").then((m) => new LanguageSupport(m.pythonLanguage)));
const rust = language(() => import("@codemirror/lang-rust").then((m) => new LanguageSupport(m.rustLanguage)));
const languages: Record<string, ReturnType<typeof language>> = {
  js: javascript, javascript, mjs: javascript, cjs: javascript,
  ts: typescript, typescript, jsx, tsx, json, css, html, htm: html, py: python, python, rs: rust, rust,
};

function find(info: string) {
  const name = info.trim().split(/\s/, 1)[0].toLowerCase();
  return Object.prototype.hasOwnProperty.call(languages, name) ? languages[name] : undefined;
}

export function ensureCodeLanguage(state: EditorState, info: string) {
  const entry = find(info);
  if (!entry || entry.support || entry.failed) return;
  entry.promise ??= entry.load().then((support) => { entry.support = support; }, () => { entry.failed = true; });
  // Gates belong to the document, not the module cache. Unopened/offscreen
  // languages must not retain every previously opened document indefinitely.
  entry.promise.then(gate(state, entry)!.resolve);
}

export const previewMarkdown = new Language(
  markdownLanguage.data,
  (markdownLanguage.parser as MarkdownParser).configure(parseCode({
    codeParser(info) {
      const entry = find(info);
      if (!entry || entry.failed) return null;
      return entry.support?.language.parser ?? ParseContext.getSkippingParser(gate(ParseContext.get()?.state, entry)?.ready);
    },
  })),
  [],
  "markdown",
);

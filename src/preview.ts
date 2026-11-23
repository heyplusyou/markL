import { type Range } from "@codemirror/state";
import { syntaxTree } from "@codemirror/language";
import { isolateHistory } from "@codemirror/commands";
import { Decoration, type DecorationSet, EditorView, ViewPlugin, type ViewUpdate, WidgetType } from "@codemirror/view";
import { ensureCodeLanguage } from "./code-languages";
import { imageDecoration, imagesChanged } from "./images";

// Decorations change only the view, never the markdown. Selection lines stay raw so
// ordinary typing, selection, copying, undo and IME input keep CodeMirror's behaviour.
const hidden = Decoration.replace({});

class Marker extends WidgetType {
  constructor(readonly text: string, readonly rule = false) {
    super();
  }

  eq(other: Marker) {
    return this.text === other.text && this.rule === other.rule;
  }

  toDOM() {
    const span = document.createElement("span");
    span.className = this.rule ? "cm-md-rule" : "cm-md-list-marker";
    span.textContent = this.text;
    if (this.rule) span.setAttribute("role", "separator");
    return span;
  }

  ignoreEvent() {
    return false; // Clicking a marker places the caret and reveals its source.
  }
}

class TaskCheckbox extends WidgetType {
  constructor(readonly from: number, readonly checked: boolean, readonly label: string) {
    super();
  }

  eq(other: TaskCheckbox) {
    return this.from === other.from && this.checked === other.checked && this.label === other.label;
  }

  updateDOM(dom: HTMLElement) {
    const input = dom as HTMLInputElement;
    input.checked = this.checked;
    input.dataset.from = String(this.from);
    input.setAttribute("aria-label", `Mark ${this.label || "task"} as ${this.checked ? "incomplete" : "complete"}`);
    return true;
  }

  toDOM(view: EditorView) {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.className = "cm-md-checkbox";
    this.updateDOM(input);
    // Don't move the editor's selection onto the task before the click arrives:
    // that would reveal its source and remove the checkbox under the pointer.
    input.addEventListener("mousedown", (event) => event.preventDefault());
    input.addEventListener("click", (event) => {
      if (view.state.readOnly) {
        event.preventDefault();
        return;
      }
      // Don't cancel the checkbox's native click: browsers would roll its
      // checked state back *after* updateDOM has already applied the new state.
      event.stopPropagation();
      // updateDOM refreshes this position after edits before the task; no stale
      // closures or DOM-to-document guesses when the widget has been reused.
      const from = Number(input.dataset.from);
      const source = view.state.doc.sliceString(from, from + 3);
      if (!/^\[[ xX]\]$/.test(source)) return;
      view.dispatch({
        changes: { from: from + 1, to: from + 2, insert: source[1] === " " ? "x" : " " },
        userEvent: "input",
        annotations: isolateHistory.of("full"),
      });
    });
    return input;
  }
}

function decorate(view: EditorView): DecorationSet {
  const { state, visibleRanges } = view;
  const { doc } = state;
  const decorations: Range<Decoration>[] = [];
  const seen = new Set<string>();
  const lines = new Map<number, Set<string>>();
  const selection = view.hasFocus ? state.selection.ranges.map((range) => ({
    from: doc.lineAt(range.from).from,
    to: doc.lineAt(range.to).to,
  })) : [];
  const editing = (from: number, to: number) => selection.some((range) => range.from <= to && range.to >= from);

  const mark = (from: number, to: number, className: string, title?: string) => {
    const decoration = Decoration.mark({ class: className, ...(title ? { attributes: { title } } : {}) });
    for (const range of visibleRanges) {
      const start = Math.max(from, range.from), end = Math.min(to, range.to);
      if (start < end) decorations.push(decoration.range(start, end));
    }
  };

  const lineClass = (from: number, to: number, className: string) => {
    for (const range of visibleRanges) {
      const end = Math.min(to, range.to);
      for (let pos = Math.max(from, range.from); pos <= end;) {
        const line = doc.lineAt(pos);
        if (!lines.has(line.from)) lines.set(line.from, new Set());
        lines.get(line.from)!.add(className);
        pos = line.to + 1;
      }
    }
  };

  // View-plugin replacements must not cross line breaks. Clip to the viewport,
  // then hide each line separately (including multiline link destinations).
  const replace = (from: number, to: number, widget?: WidgetType) => {
    for (const range of visibleRanges) {
      const end = Math.min(to, range.to);
      for (let pos = Math.max(from, range.from); pos < end;) {
        const line = doc.lineAt(pos), upto = Math.min(end, line.to);
        if (pos < upto) decorations.push((widget ? Decoration.replace({ widget }) : hidden).range(pos, upto));
        pos = line.to + 1;
      }
    }
  };

  const tree = syntaxTree(state);
  for (const range of visibleRanges) {
    tree.iterate({
      from: range.from,
      to: range.to,
      enter(ref) {
        const node = ref.node;
        const { name, from, to } = node;
        if (["HTMLBlock", "HTMLTag", "Table", "LinkReference"].includes(name)) return false;
        // A long wrapped line can split visibleRanges. Visit each enclosing
        // node once, but still descend to find children in later ranges.
        const key = `${name}:${from}:${to}`;
        if (seen.has(key)) return;
        seen.add(key);
        const active = editing(from, to);
        if (name === "FencedCode" || name === "CodeBlock") {
          lineClass(from, to, "cm-md-code-block");
          lineClass(from, from, "cm-md-code-start");
          lineClass(Math.max(from, to - 1), to, "cm-md-code-end");
          const info = node.getChild("CodeInfo");
          if (info) {
            ensureCodeLanguage(state, doc.sliceString(info.from, Math.min(info.to, info.from + 64)));
            mark(info.from, info.to, "cm-md-code-label");
          }
          for (let child = node.firstChild; child; child = child.nextSibling) {
            if (child.name === "CodeMark") {
              lineClass(child.from, child.to, "cm-md-code-fence");
              if (!active) replace(child.from, child.to);
            }
          }
          return false; // Nested code has its own syntax highlighter, never Markdown preview.
        }
        if (name === "Image") {
          const url = node.getChild("URL"), marks = node.getChildren("LinkMark");
          if (!active && url && marks.length > 1 && to - from < 4096 && doc.lineAt(from).number === doc.lineAt(to).number) {
            replace(from, to, imageDecoration(view, doc.sliceString(url.from, url.to), doc.sliceString(marks[0].to, marks[1].from)));
          }
          return false;
        }
        const heading = /^(ATX|Setext)Heading([1-6])$/.exec(name);
        if (heading) {
          const underline = heading[1] === "Setext" ? node.getChild("HeaderMark") : null;
          const end = underline ? Math.max(from, underline.from - 1) : to;
          lineClass(from, end, `cm-md-heading cm-md-h${heading[2]}`);
          if (!active) {
            for (let child = node.firstChild; child; child = child.nextSibling) {
              if (child.name !== "HeaderMark") continue;
              let start = child.from, end = child.to;
              if (heading[1] === "ATX") {
                if (start === from && doc.sliceString(end, end + 1) === " ") end++;
                else if (start > from && doc.sliceString(start - 1, start) === " ") start--;
              }
              replace(start, end);
            }
          }
        } else if (name === "StrongEmphasis" || name === "Emphasis" || name === "Strikethrough" || name === "InlineCode") {
          const className = {
            StrongEmphasis: "cm-md-strong", Emphasis: "cm-md-emphasis",
            Strikethrough: "cm-md-strike", InlineCode: "cm-md-code",
          }[name]!;
          mark(from, to, className);
          if (!active) {
            for (let child = node.firstChild; child; child = child.nextSibling) {
              if (["EmphasisMark", "StrikethroughMark", "CodeMark"].includes(child.name)) replace(child.from, child.to);
            }
          }
        } else if (name === "Link" || name === "Autolink") {
          const url = node.getChild("URL");
          // Lezer also calls bare [brackets] and unresolved references "Link".
          // Keep those literal rather than invent a destination or scan the
          // whole file for reference definitions on every viewport update.
          if (!url) return;
          mark(from, to, "cm-md-link", doc.sliceString(url.from, url.to));
          if (!active) {
            if (name === "Autolink") {
              replace(from, from + 1);
              replace(to - 1, to);
            } else {
              replace(from, from + 1); // opening [
              // The second LinkMark closes the label; everything after it is
              // destination/title/reference syntax, not the displayed label.
              const close = node.getChildren("LinkMark")[1];
              if (close) replace(close.from, to);
            }
          }
        } else if (name === "Blockquote") {
          lineClass(from, to, "cm-md-quote");
        } else if (name === "QuoteMark") {
          if (!active) replace(from, to + (doc.sliceString(to, to + 1) === " " ? 1 : 0));
        } else if (name === "ListMark" && !active) {
          const task = node.parent?.getChild("Task");
          if (task) replace(from, task.from);
          else {
            const source = doc.sliceString(from, to);
            replace(from, to, new Marker(/^\d/.test(source) ? source.replace(/\)$/, ".") : "•"));
          }
        } else if (name === "TaskMarker" && !active) {
          const checked = doc.sliceString(from + 1, from + 2) !== " ";
          const label = doc.sliceString(to, Math.min(doc.lineAt(to).to, to + 160)).trim();
          replace(from, to, new TaskCheckbox(from, checked, label));
          if (checked && node.parent) mark(to, node.parent.to, "cm-md-task-complete");
        } else if (name === "HorizontalRule" && !active) {
          replace(from, to, new Marker("", true));
        }
      },
    });
  }
  for (const [from, classes] of lines) {
    decorations.push(Decoration.line({ class: [...classes].join(" ") }).range(from));
  }
  return Decoration.set(decorations, true);
}

export const inlinePreview = ViewPlugin.fromClass(class {
  decorations: DecorationSet;

  constructor(view: EditorView) {
    this.decorations = decorate(view);
  }

  update(update: ViewUpdate) {
    if (update.docChanged || update.selectionSet || update.viewportChanged || update.focusChanged
      || imagesChanged(update.startState, update.state)
      || syntaxTree(update.startState) !== syntaxTree(update.state)) {
      this.decorations = decorate(update.view);
    }
  }
}, { decorations: (plugin) => plugin.decorations });

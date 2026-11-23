import { StateEffect, StateField, type Text } from "@codemirror/state";
import { syntaxTree } from "@codemirror/language";
import { type SyntaxNode } from "@lezer/common";
import { Decoration, type DecorationSet, EditorView, ViewPlugin, type ViewUpdate, WidgetType } from "@codemirror/view";

type Part = { text?: string; tag?: "strong" | "em" | "s" | "code" | "span"; title?: string; children?: Part[] };
type Cell = { offset: number; parts: Part[] };
type Block = { from: number; to: number; widget: TableWidget };

function parts(doc: Text, parent: SyntaxNode, from = parent.from, to = parent.to, depth = 0): Part[] {
  if (depth > 8) return [{ text: doc.sliceString(from, to) }];
  const result: Part[] = [];
  let pos = from;
  for (let child = parent.firstChild; child; child = child.nextSibling) {
    if (child.to <= from || child.from >= to) continue;
    if (child.from > pos) result.push({ text: doc.sliceString(pos, child.from) });
    const first = child.firstChild, last = child.lastChild;
    const tag = { StrongEmphasis: "strong", Emphasis: "em", Strikethrough: "s", InlineCode: "code" }[child.name] as Part["tag"];
    if (tag && first && last) {
      result.push({ tag, children: parts(doc, child, first.to, last.from, depth + 1) });
    } else if (child.name === "Link" && child.getChild("URL")) {
      const marks = child.getChildren("LinkMark"), url = child.getChild("URL")!;
      result.push({ tag: "span", title: doc.sliceString(url.from, url.to),
        children: parts(doc, child, marks[0].to, marks[1].from, depth + 1) });
    } else {
      const text = doc.sliceString(child.from, child.to);
      result.push({ text: child.name === "Escape" ? text.slice(1) : text });
    }
    pos = child.to;
  }
  if (pos < to) result.push({ text: doc.sliceString(pos, to) });
  return result;
}

function append(parent: HTMLElement, values: readonly Part[]) {
  for (const part of values) {
    if (!part.tag) {
      parent.append(document.createTextNode(part.text ?? ""));
    } else {
      const element = document.createElement(part.tag);
      if (part.title) {
        element.title = part.title;
        element.className = "cm-md-link";
      }
      if (part.children) append(element, part.children);
      parent.append(element);
    }
  }
}

class TableWidget extends WidgetType {
  constructor(readonly source: string, readonly rows: Cell[][], readonly align: string[]) {
    super();
  }

  eq(other: TableWidget) {
    return this === other;
  }

  get estimatedHeight() {
    return this.rows.length * 35 + 16;
  }

  toDOM(view: EditorView) {
    const wrapper = document.createElement("div");
    wrapper.className = "cm-md-table-preview";
    wrapper.tabIndex = 0;
    wrapper.setAttribute("role", "group");
    wrapper.setAttribute("aria-label", "Markdown table. Press Enter to edit.");
    const table = document.createElement("table");
    const head = table.createTHead(), body = table.createTBody();
    this.rows.forEach((cells, index) => {
      const row = (index === 0 ? head : body).insertRow();
      for (let col = 0; col < this.align.length; col++) {
        const cell = cells[col];
        const element = document.createElement(index === 0 ? "th" : "td");
        if (index === 0) element.setAttribute("scope", "col");
        element.style.textAlign = this.align[col];
        element.dataset.offset = String(cell?.offset ?? cells[0]?.offset ?? 0);
        if (cell) append(element, cell.parts);
        row.append(element);
      }
    });
    const edit = (offset: number) => {
      const block = view.state.field(tableState).blocks.find((item) => item.widget === this);
      if (!block) return;
      view.dispatch({ selection: { anchor: Math.min(block.to, block.from + offset) }, scrollIntoView: true });
      view.focus();
    };
    wrapper.addEventListener("mousedown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      const cell = (event.target as HTMLElement).closest<HTMLElement>("[data-offset]");
      edit(Number(cell?.dataset.offset ?? 0));
    });
    wrapper.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        edit(this.rows[0]?.[0]?.offset ?? 0);
      }
    });
    wrapper.append(table);
    return wrapper;
  }
}

const setTables = StateEffect.define<Block[]>();
const empty = { blocks: [] as Block[], decorations: Decoration.none };

function decorations(blocks: readonly Block[], viewState: EditorView["state"]) {
  return Decoration.set(blocks.filter((block) => !viewState.selection.ranges.some((range) => (
    range.from <= block.to && range.to >= block.from
  ))).map((block) => Decoration.replace({ widget: block.widget, block: true }).range(block.from, block.to)), true);
}

// Block replacements must be provided directly, not from a viewport decoration
// callback. Selection reveals a table synchronously, before the next paint.
const tableState = StateField.define<{ blocks: Block[]; decorations: DecorationSet }>({
  create: () => empty,
  update(value, tr) {
    let blocks = value.blocks;
    if (tr.docChanged) {
      blocks = blocks.filter((block) => !tr.changes.touchesRange(block.from, block.to)).map((block) => ({
        from: tr.changes.mapPos(block.from, 1), to: tr.changes.mapPos(block.to, -1), widget: block.widget,
      }));
    }
    for (const effect of tr.effects) if (effect.is(setTables)) blocks = effect.value;
    if (blocks === value.blocks && !tr.selection && !tr.docChanged) return value;
    return { blocks, decorations: decorations(blocks, tr.state) };
  },
  provide: (field) => EditorView.decorations.from(field, (value) => value.decorations),
});

function collect(view: EditorView): Block[] {
  const blocks: Block[] = [], previous = view.state.field(tableState).blocks;
  const { doc } = view.state;
  syntaxTree(view.state).iterate({
    from: view.viewport.from, to: view.viewport.to,
    enter(ref) {
      if (["FencedCode", "CodeBlock", "HTMLBlock"].includes(ref.name)) return false;
      if (ref.name !== "Table") return;
      const node = ref.node;
      // Bound both parsing and DOM work. Huge and nested tables stay editable
      // source; no unbounded block widgets in a many-megabyte document.
      if (node.parent?.name !== "Document" || node.to - node.from > 32768) return false;
      const from = doc.lineAt(node.from).from, to = node.to;
      const source = doc.sliceString(from, to);
      const old = previous.find((block) => block.from === from && block.widget.source === source);
      if (old) { blocks.push(old); return false; }
      const rows: Cell[][] = [];
      let align: string[] = [], count = 0;
      for (let row = node.firstChild; row; row = row.nextSibling) {
        if (row.name === "TableDelimiter") {
          const text = doc.sliceString(row.from, row.to).trim().replace(/^\||\|$/g, "");
          align = text.split("|").map((value) => /^:\s*-+\s*:$/.test(value.trim()) ? "center"
            : /:$/.test(value.trim()) ? "right" : "left");
        }
        if (row.name !== "TableHeader" && row.name !== "TableRow") continue;
        if (rows.length >= 100) return false;
        const cells: Cell[] = [];
        const pipes: number[] = [];
        let leading = false;
        for (let cell = row.firstChild; cell; cell = cell.nextSibling) {
          if (cell.name === "TableDelimiter") {
            if (!pipes.length && !cells.length && !doc.sliceString(row.from, cell.from).trim()) leading = true;
            pipes.push(cell.to);
            continue;
          }
          if (cell.name !== "TableCell") continue;
          const column = pipes.length - (leading ? 1 : 0);
          if (++count > 1024 || column >= 32) return false;
          cells[column] = { offset: cell.from - from, parts: parts(doc, cell) };
        }
        // Lezer omits empty TableCell nodes. Delimiters, not the count of
        // nonempty cells, determine column positions.
        const trailing = row.lastChild?.name === "TableDelimiter";
        const columns = Math.max(cells.length, align.length, pipes.length + 1 - Number(leading) - Number(trailing));
        if (columns > 32) return false;
        count += columns - cells.filter(Boolean).length;
        if (count > 1024) return false;
        for (let col = 0; col < columns; col++) {
          cells[col] ??= { offset: (col === 0 && !leading ? row.from : (pipes[col - 1 + Number(leading)] ?? row.to)) - from, parts: [] };
        }
        rows.push(cells);
      }
      if (rows.length && align.length && align.length <= 32) blocks.push({ from, to, widget: new TableWidget(source, rows, align) });
      return false;
    },
  });
  return blocks;
}

const syncTables = ViewPlugin.fromClass(class {
  frame = 0;
  from: number;
  to: number;
  noCandidates = false;
  constructor(view: EditorView) {
    this.from = view.viewport.from;
    this.to = view.viewport.to;
    this.schedule(view);
  }
  schedule(view: EditorView) {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      const blocks = collect(view), old = view.state.field(tableState).blocks;
      const { from, to } = view.viewport;
      this.noCandidates = !blocks.length && to - from <= 32768 && !view.state.doc.sliceString(from, to).includes("|");
      if (blocks.length !== old.length || blocks.some((block, i) => block.from !== old[i].from
        || block.to !== old[i].to || block.widget !== old[i].widget)) {
        view.dispatch({ effects: setTables.of(blocks) });
      }
    });
  }
  update(update: ViewUpdate) {
    const viewport = update.view.viewport;
    const moved = viewport.from !== update.changes.mapPos(this.from, -1)
      || viewport.to !== update.changes.mapPos(this.to, 1);
    this.from = viewport.from;
    this.to = viewport.to;
    let pipe = false;
    if (this.noCandidates && update.docChanged) update.changes.iterChanges((_from, _to, _newFrom, _newTo, inserted) => {
      pipe ||= inserted.length > 32768 || inserted.toString().includes("|");
    });
    // A viewport without pipes cannot acquire a table through an ordinary key
    // press. Avoid a second syntax-tree traversal on every edit in huge files.
    if (this.noCandidates && !moved && !pipe) return;
    if (update.docChanged || update.selectionSet || moved
      || syntaxTree(update.startState) !== syntaxTree(update.state)) this.schedule(update.view);
  }
  destroy() { cancelAnimationFrame(this.frame); }
});

export const tablePreview = [tableState, syncTables];

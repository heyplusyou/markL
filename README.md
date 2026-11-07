# markL

A lightweight markdown editor for macOS, Windows and Linux. It starts instantly, stays small in memory, edits
plain `.md` files where they already are, and keeps the files you worked on recently in a sidebar.

**Status:** early. You can open, edit and save markdown files (File → New, Open, Save, Save As, or
`markl notes.md`) and reopen them from the recent-files sidebar. Inline rendering comes next.

## What it does

- Renders markdown as you type: headings, emphasis, links, lists, code and images look like the result, and the
  markdown syntax appears only where the cursor is. The file on disk stays plain markdown, exactly as you wrote it.
- Stays fast with very large files.
- Shows the last 20 files you opened or saved in a sidebar, most recent first. Click one to open it. Files that
  were moved or deleted are crossed out, and the × next to a file removes it from the list.
- Opens files from the file dialog, by drag and drop, from the command line (`markl notes.md`) and through the
  system's "Open with".
- Saves to the file itself. No database, no vault, no import step: markL only remembers which files you opened.
- Asks before closing or replacing a document with unsaved changes.
- Opens UTF-8 files and saves them with the line endings and byte order mark they had.
- Notices when a file changes on disk and reloads it, unless you have unsaved changes.

markL is an editor, not a notes app: no sync, accounts, plugins, backlinks or knowledge graph. It has no
spellcheck.

Platforms, in order of priority: macOS, Windows, Linux.

## Performance budget

These targets define markL; a change that breaks them doesn't ship. markL runs in the system webview, which
has a fixed cost of its own: on macOS, about 250 ms of startup and 30 MB of memory belong to WebKit. The targets
were set from an empty window (250 ms to the window, 380 ms to a loaded page, 56 MB).

| | Target (macOS) | Today |
| --- | --- | --- |
| Launch to window on screen | under 300 ms | 260 ms |
| Launch to editable, with a 100 KB file open | under 450 ms | 425 ms |
| Memory with one 100 KB file open, all processes | under 85 MB | 71 MB |
| Keypress to screen, in files up to 1 MB | under 16 ms | 5 ms* |
| Open a 10 MB file | under 500 ms, scrolling stays smooth | 457 ms |
| App size | under 10 MB | 4.0 MB |

Measured on an Apple M2 (24 GB), macOS 26.2, warm launches, median of 5 runs, on 2026-10-23. The sidebar
(step 3) was measured afterwards against a build without it, alternating launches on a busy machine: it adds
0.5 MB and no measurable startup time. \*Typing is measured in headless
Chromium with the editor alone (`tests/frontend.py`), because the real app can't yet be driven from a script; at
the end of a 10 MB file it is 11 ms. Windows and Linux targets follow once markL has been measured there.

## Tech stack

- **[Tauri 2](https://tauri.app)**: a native Rust app that shows its UI in the system webview (WKWebView on macOS,
  WebView2 on Windows, WebKitGTK on Linux). No bundled browser engine, so the download stays a few MB.
- **Rust** for everything that touches the system: reading files, atomic saves, the recent-files list, watching
  files for changes ([notify](https://crates.io/crates/notify)), and files passed on the command line or through
  "Open with" (single-instance plugin).
- **[CodeMirror 6](https://codemirror.net)** with its markdown language package as the editor. The inline
  rendering works like Obsidian's live preview: the document stays the markdown text, and CodeMirror decorations
  change how it looks. Files are saved exactly as written, and CodeMirror only draws the visible part of a
  document, which keeps large files fast.
- **TypeScript without a UI framework**, built with Vite, with bun as the package manager. The UI is a sidebar and
  an editor; a framework would add weight without adding anything.

Why Tauri and not a native Rust UI: inline rendering needs a rich-text engine, with mixed font sizes, hidden
syntax and inline images. A webview has one; GPUI or iced would need one built from scratch. The cost is memory:
the webview runs in separate processes, and on Windows WebView2 is based on Chromium.

Why not ProseMirror, Milkdown or Tiptap (Typora-style editors): they convert markdown into their own document
model and write it back, which reformats files. They also put the whole document into the page, which is slow
for large files.

## Development

Needs [Rust](https://rustup.rs), [bun](https://bun.sh) and, on macOS, the Xcode command line tools.

```sh
bun install
bun run tauri dev                    # development build with hot reload
bun run tauri build --bundles app    # release build: src-tauri/target/release/bundle/macos/markL.app
bun run measure                      # startup time, memory and size of the release build (macOS)
bun run measure --file bench/fixtures/100k.md   # the same with a file open
bun run fixtures                     # test files of 100 KB, 1 MB and 10 MB in bench/fixtures
bun run test                         # frontend tests (Python Playwright, Chromium) and Rust tests
```

## Roadmap

1. ~~Skeleton: a Tauri app with one window, measured on macOS~~ (done). The Windows measurement is still open
2. ~~Open, edit and save one file~~ (done)
3. ~~Recent-files sidebar~~ (done)
4. Inline rendering: headings, emphasis, links, inline code, lists and task lists, quotes, rules
5. Inline rendering: code blocks with highlighting, images, tables
6. Open by drag and drop, with "Open with", and from the command line while markL is running (opening a file at
   startup works)
7. Reload on external changes
8. Packages for macOS (signed, notarized), Windows and Linux

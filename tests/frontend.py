#!/usr/bin/env python3
"""Tests the frontend in Chromium (headless Playwright), with Tauri's IPC replaced by a mock.

Covers what the Rust unit tests can't: typing, the unsaved-changes flags, open/save/close flows and
their prompts, that saving sends exactly the editor's text, and typing latency in large files.
Native dialogs and file access are Rust's and are mocked here.

Run after `bun run build` (and `bun run fixtures` for the typing latency):
    python3 tests/frontend.py [--screenshots DIR]
"""

import argparse
import functools
import json
import http.server
import statistics
import sys
import threading
import struct
import zlib
from urllib.parse import unquote
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
FIXTURES = ROOT / "bench/fixtures"
MOD = "Meta" if sys.platform == "darwin" else "Control"

# Stands in for Tauri's IPC bridge. Records every command; tests set what the "Rust side" answers.
MOCK = """
window.mock = { calls: [], handlers: {}, listeners: {}, answer: "cancel", nextOpen: null, startup: null,
  saved: null, recent: [], files: {}, imageEpoch: 1, pending: [], requestId: 0 };
let nextId = 1;
window.__TAURI_INTERNALS__ = {
  metadata: { currentWindow: { label: "main" }, currentWebview: { windowLabel: "main", label: "main" } },
  transformCallback(callback) { const id = nextId++; mock.handlers[id] = callback; return id; },
  unregisterCallback(id) { delete mock.handlers[id]; },
  convertFileSrc(path, protocol) {
    mock.calls.push(["image_url", {path, protocol}]);
    return `${location.origin}/__markl_image/${encodeURIComponent(path)}`;
  },
  async invoke(cmd, args) {
    const raw = args instanceof Uint8Array;
    mock.calls.push([cmd, raw ? new TextDecoder().decode(args) : args]);
    const bytes = (text) => new TextEncoder().encode(text).buffer;
    switch (cmd) {
      case "plugin:event|listen":
        mock.listeners[args.event] = args.handler;
        if (args.event === "command") mock.listener = args.handler;
        return args.handler;
      case "open_startup_file":
        if (mock.startup != null) return bytes(mock.startup);
        if (mock.pending.length) return bytes(mock.pending.shift().text);
        throw "no file";
      case "open_file": if (mock.nextOpen == null) throw "cancelled"; return bytes(mock.nextOpen);
      case "next_open_request":
        if (!mock.pending.length && mock.onEmptyRequest) {
          const text = mock.onEmptyRequest; mock.onEmptyRequest = null; mock.queueFile(text); return null;
        }
        return mock.pending[0]?.id ?? null;
      case "open_pending": {
        if (mock.pending[0]?.id !== args.id) throw "no such pending request";
        const request = mock.pending.shift();
        if (mock.readDelay) await new Promise(resolve => setTimeout(resolve, mock.readDelay));
        if (request.error) throw "read failed";
        return bytes(request.text);
      }
      case "cancel_pending_open": mock.pending = []; return null;
      case "ready":
        if (mock.pending.length) queueMicrotask(() => mock.emitEvent("open-request", null));
        return null;
      case "save":
        if (mock.saveDelay) await new Promise(resolve => setTimeout(resolve, mock.saveDelay));
        if (mock.saveError) throw "save failed or cancelled";
        mock.saved = new TextDecoder().decode(args); return null;
      case "save_as": mock.imageEpoch++; mock.saved = new TextDecoder().decode(args); return null;
      case "image_context": {
        const epoch = mock.imageEpoch;
        if (mock.imageDelay) await new Promise((resolve) => setTimeout(resolve, mock.imageDelay));
        return epoch;
      }
      case "ask_save_changes":
        if (mock.askDelay) await new Promise(resolve => setTimeout(resolve, mock.askDelay));
        return mock.answer;
      case "recent_files": return mock.recent;
      case "open_recent":
        if (!(args.path in mock.files)) throw "can't be opened";
        mock.recent = mock.recent.map((file) => ({ ...file, current: file.path === args.path }));
        return bytes(mock.files[args.path]);
      case "remove_recent": mock.recent = mock.recent.filter((file) => file.path !== args.path); return null;
      default: return null;
    }
  },
};
mock.emit = (payload) => mock.handlers[mock.listener]({ event: "command", id: 1, payload });
mock.emitEvent = (event, payload) => mock.handlers[mock.listeners[event]]?.({event, id: 1, payload});
mock.queueFile = (text, error = false) => {
  mock.pending.push({id: ++mock.requestId, text, error});
  mock.emitEvent("open-request", null);
};
// What EditorView.findFromDOM does; the property is internal to CodeMirror (cmView before 6.43).
mock.view = () => { const el = document.querySelector(".cm-content"); return el.cmTile ? el.cmTile.root.view : el.cmView.view; };
mock.text = () => mock.view().state.doc.toString();
mock.commands = (since = 0) => mock.calls.slice(since).map(([cmd]) => cmd).filter((cmd) => !cmd.startsWith("plugin:"));
mock.edited = () => mock.calls.filter(([cmd]) => cmd === "set_edited").map(([, args]) => args.edited);
mock.args = (name) => mock.calls.filter(([cmd]) => cmd === name).at(-1)?.[1];
mock.sidebar = () => [...document.querySelectorAll("#sidebar li")].map((li) => ({
  name: li.querySelector(".name").textContent, dir: li.querySelector(".dir").textContent,
  current: li.classList.contains("current"), missing: li.classList.contains("missing"),
}));
"""


def sample_png():
    """Small, deterministic local-image fixture; no image-library dependency."""
    width, height = 320, 120
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            bar = any(left <= x < left + 48 and top <= y < 104 for left, top in ((32, 70), (112, 44), (192, 18)))
            raw.extend((42, 112, 186) if bar else (238, 243, 249))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PNG_FIXTURE = sample_png()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/__markl_image/"):
            payload = unquote(self.path.split("/__markl_image/", 1)[1])
            source = payload.split("/", 1)[-1]
            if source not in ("image.png", "images/plot.png", "images/a%20b.png", "images/a b.png"):
                self.send_error(404)
                return
            data = PNG_FIXTURE
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()


def serve(directory):
    handler = functools.partial(QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# A recent list as Rust would send it, and the files behind it.
RECENT = [
    {"path": "/Users/me/notes/today.md", "name": "today.md", "dir": "~/notes", "exists": True, "current": True},
    {"path": "/Users/me/work/plan.md", "name": "plan.md", "dir": "~/work", "exists": True, "current": False},
    {"path": "/Volumes/Old/gone.md", "name": "gone.md", "dir": "/Volumes/Old", "exists": False, "current": False},
    {"path": "/tmp/<img src=x onerror=alert(1)>.md", "name": "<img src=x onerror=alert(1)>.md", "dir": "/tmp",
     "exists": True, "current": False},
]
FILES = {"/Users/me/notes/today.md": "# Today\n", "/Users/me/work/plan.md": "# Plan\n\n- [ ] ship\n"}


class App:
    def __init__(self, browser, url, startup=None, recent=(), files=None, pending=(), **page_options):
        self.page = browser.new_page(**page_options)
        self.errors = []
        self.requests = []
        self.scripts = []
        self.page.on("request", lambda request: self.requests.append(request.url))
        self.page.on("request", lambda request: self.scripts.append(request.url) if request.resource_type == "script" else None)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("console", lambda message: self.errors.append(message.text) if message.type == "error" else None)
        self.page.add_init_script(MOCK)
        self.page.add_init_script(f"mock.recent = {json.dumps(list(recent))}; mock.files = {json.dumps(files or {})};")
        self.page.add_init_script(f"mock.pending = {json.dumps(list(pending))}; mock.requestId = 100;")
        if startup is not None:
            self.page.add_init_script(f"mock.startup = {json.dumps(startup)};")
        self.page.goto(url)
        self.page.wait_for_function("mock.commands().includes('ready')")

    def js(self, expression):
        return self.page.evaluate(expression)

    def command(self, name):
        calls = self.js("mock.calls.length")
        self.js(f"mock.emit({name!r})")
        self.page.wait_for_timeout(50)
        return self.js(f"mock.commands({calls})")

    def act(self, action):
        """Runs `action` (a function) and returns the commands it led to."""
        calls = self.js("mock.calls.length")
        action()
        self.page.wait_for_timeout(50)
        return self.js(f"mock.commands({calls})")

    def recent(self, name):
        return self.page.locator("#sidebar li", has_text=name)

    def type(self, text):
        self.page.locator(".cm-content").click()
        self.page.keyboard.type(text)


def typing_latency(browser, url, text):
    """Milliseconds from keydown until the frame after it, for each key typed at the end of `text`."""
    app = App(browser, url, startup=text)
    app.js("""
        window.latencies = [];
        document.addEventListener("keydown", () => {
          const start = performance.now();
          requestAnimationFrame(() => setTimeout(() => latencies.push(performance.now() - start)));
        }, true);
    """)
    app.page.locator(".cm-content").click()
    app.page.keyboard.press(f"{MOD}+End")
    app.page.wait_for_timeout(5000)  # let the background parse reach the viewport
    for char in "typing at the end of the file":
        app.page.keyboard.press(char if char != " " else "Space")
        app.page.wait_for_timeout(20)
    latencies = app.js("latencies")
    app.page.close()
    return latencies


def sidebar(browser, url):
    results = []
    app = App(browser, url, recent=RECENT, files=FILES)
    app.page.wait_for_selector("#sidebar li")
    items = app.js("mock.sidebar()")
    results.append(check(
        "sidebar lists recent files, marks the current and the missing one",
        [item["name"] for item in items] == [file["name"] for file in RECENT]
        and [item["current"] for item in items] == [True, False, False, False]
        and [item["missing"] for item in items] == [False, False, True, False]
        and items[2]["dir"] == "Moved or deleted",
        items,
    ))
    results.append(check(
        "file names are shown as text, not HTML",
        app.js("document.querySelectorAll('#sidebar img').length") == 0
        and items[3]["name"] == RECENT[3]["name"],
    ))

    commands = app.act(lambda: app.recent("plan.md").locator(".file").click())
    results.append(check(
        "clicking a recent file opens it and updates the list",
        commands[:1] == ["open_recent"] and "recent_files" in commands
        and app.js("mock.args('open_recent').path") == "/Users/me/work/plan.md"
        and app.js("mock.text()") == FILES["/Users/me/work/plan.md"]
        and app.js("mock.sidebar()[1].current") is True,
        [commands, app.js("mock.text()")],
    ))

    commands = app.act(lambda: app.recent("plan.md").locator(".file").click())
    results.append(check("clicking the current file does nothing", commands == [], commands))

    app.type("edit")
    app.js("mock.answer = 'cancel'")
    commands = app.act(lambda: app.recent("today.md").locator(".file").click())
    results.append(check(
        "with unsaved changes, clicking a recent file asks first; Cancel keeps the text",
        commands == ["ask_save_changes"] and app.js("mock.text()").endswith("edit"),
        commands,
    ))

    def remove():
        app.recent("gone.md").hover()
        app.recent("gone.md").locator(".remove").click()

    commands = app.act(remove)
    results.append(check(
        "the remove button takes a file off the list",
        commands == ["remove_recent", "recent_files"]
        and "gone.md" not in [item["name"] for item in app.js("mock.sidebar()")],
        commands,
    ))
    app.page.close()

    app = App(browser, url)
    results.append(check(
        "an empty list shows a hint",
        app.page.locator("#sidebar .empty").is_visible() and app.js("mock.sidebar()") == [],
    ))
    app.page.close()
    return results


def check(name, condition, detail=""):
    print(f"{'ok  ' if condition else 'FAIL'} {name}{'' if condition else f': {detail!r}'}")
    return condition


PREVIEW_SAMPLE = (
    "Editing here\n\n# Heading\n\n"
    "**bold** and *italic* and ~~gone~~ and `code`\n\n"
    '[site](https://example.com "Title")\n\n'
    "- item\n1. numbered\n- [ ] ship\n- [x] done\n\n"
    "> quoted **words**\n\n---\n\n"
    "```md\n# literal *code*\n- [ ] not a task\n```\n\n"
    "![alt](image.png)\n\n<em>literal HTML</em>\n"
)


def preview(browser, url):
    results = []
    app = App(browser, url, startup=PREVIEW_SAMPLE, viewport={"width": 1100, "height": 1300})
    content = app.page.locator(".cm-content")
    app.page.wait_for_selector(".cm-md-checkbox")
    rendered = content.inner_text()
    results.append(check(
        "inline preview renders headings, emphasis, code, links, lists, quotes and rules",
        app.page.locator(".cm-md-h1").inner_text() == "Heading"
        and "**bold**" not in rendered and "~~gone~~" not in rendered and "`code`" not in rendered
        and "https://example.com" not in rendered and "site" in rendered
        and "• item" in rendered and "1. numbered" in rendered
        and "> quoted" not in rendered and "quoted words" in rendered
        and app.page.locator(".cm-md-rule").count() == 1
        and app.page.locator(".cm-md-checkbox").count() == 2,
        rendered,
    ))
    results.append(check(
        "code text and HTML stay literal while local images render safely",
        "# literal *code*" in rendered and "- [ ] not a task" in rendered
        and app.page.locator(".cm-md-image").count() == 1 and "<em>literal HTML</em>" in rendered
        and content.locator("em, a[href], script").count() == 0,
    ))
    results.append(check(
        "rendering never changes the markdown or marks it edited",
        app.js("mock.text()") == PREVIEW_SAMPLE and app.js("mock.edited()") == [],
    ))

    def select(from_, to=None):
        app.js(f"mock.view().dispatch({{selection: {{anchor: {from_}, head: {to if to is not None else from_}}}}}); mock.view().focus()")
        app.page.wait_for_timeout(30)

    heading = PREVIEW_SAMPLE.index("# Heading")
    select(heading + 3)
    results.append(check(
        "moving the caret into a heading reveals its source without editing",
        app.page.locator(".cm-md-h1").inner_text() == "# Heading"
        and "https://example.com" not in content.inner_text()
        and app.js("mock.text()") == PREVIEW_SAMPLE and app.js("mock.edited()") == [],
    ))
    select(heading + 2)
    app.page.keyboard.type("New ")
    results.append(check(
        "typing into revealed markup edits the source at the caret",
        app.js("mock.text()") == PREVIEW_SAMPLE.replace("# Heading", "# New Heading")
        and app.page.locator(".cm-md-h1").inner_text() == "# New Heading",
    ))
    app.page.keyboard.press(f"{MOD}+z")
    select(heading, heading + 2)
    app.page.keyboard.press("Backspace")
    results.append(check(
        "removing a heading marker removes its rendering on reparse",
        app.page.locator(".cm-md-h1").count() == 0
        and app.js("mock.text()") == PREVIEW_SAMPLE.replace("# Heading", "Heading"),
    ))
    app.page.keyboard.press(f"{MOD}+z")
    select(0)
    results.append(check(
        "undo restores both the original text and inline rendering",
        app.js("mock.text()") == PREVIEW_SAMPLE
        and app.page.locator(".cm-md-h1").inner_text() == "Heading"
        and app.js("mock.edited().at(-1)") is False,
    ))
    start = PREVIEW_SAMPLE.index("**bold**")
    end = PREVIEW_SAMPLE.index("- item") - 1
    select(start, end)
    results.append(check(
        "a multiline selection reveals all touched syntax, including link destinations",
        "**bold**" in content.inner_text() and '[site](https://example.com "Title")' in content.inner_text()
        and app.page.locator(".cm-md-h1").inner_text() == "Heading",
    ))
    select(0)
    app.page.locator(".cm-md-h1").click()
    results.append(check(
        "clicking rendered text puts the caret into editable markdown",
        app.page.locator(".cm-md-h1").inner_text() == "# Heading" and app.js("mock.edited().at(-1)") is False,
    ))

    select(0)
    app.js("mock.calls = []")
    checkbox = app.page.locator(".cm-md-checkbox").first
    checkbox.click()
    toggled = PREVIEW_SAMPLE.replace("- [ ] ship", "- [x] ship")
    results.append(check(
        "clicking a task checkbox changes only its marker and marks the document edited",
        app.js("mock.text()") == toggled and checkbox.is_checked() and app.js("mock.edited()") == [True],
        app.js("mock.text()"),
    ))
    app.page.keyboard.press(f"{MOD}+z")
    results.append(check(
        "task checkbox changes undo back to the byte-exact original",
        app.js("mock.text()") == PREVIEW_SAMPLE and not checkbox.is_checked()
        and app.js("mock.edited()") == [True, False],
    ))
    checkbox.click()
    checkbox.click()
    app.page.keyboard.press(f"{MOD}+z")
    results.append(check(
        "rapid task toggles are separate undo steps",
        app.js("mock.text()") == toggled and checkbox.is_checked(),
    ))
    app.page.keyboard.press(f"{MOD}+z")
    app.js("mock.view().dispatch({changes: {from: 0, insert: 'prefix '}})")
    checkbox.click()
    results.append(check(
        "checkbox positions stay correct after edits before the task",
        app.js("mock.text()") == "prefix " + toggled and checkbox.is_checked(),
    ))
    app.page.keyboard.press(f"{MOD}+z")
    app.page.keyboard.press(f"{MOD}+z")
    checkbox.focus()
    checkbox.press("Space")
    results.append(check(
        "task checkboxes support keyboard activation and accessible labels",
        app.js("mock.text()") == toggled and checkbox.is_checked()
        and "ship" in checkbox.get_attribute("aria-label"),
    ))
    app.command("save")
    results.append(check(
        "saving the rendered document sends the exact markdown, not preview HTML",
        app.js("mock.saved") == toggled and app.js("mock.edited().at(-1)") is False,
    ))
    select(0, len(toggled))
    copied = app.js("""(() => {
      const clipboardData = new DataTransfer();
      const event = new ClipboardEvent("copy", {clipboardData, bubbles: true, cancelable: true});
      document.querySelector(".cm-content").dispatchEvent(event);
      return clipboardData.getData("text/plain");
    })()""")
    results.append(check(
        "copying the rendered document preserves the original markdown syntax",
        copied == toggled and app.js("mock.text()") == toggled,
        copied,
    ))
    results.append(check("inline preview has no browser or plugin errors", not app.errors, app.errors))
    app.page.close()

    tricky = (
        "Editing here\n\nSetext heading\n==============\n\n"
        "***nested*** and \\*escaped\\* and **unfinished\n\n"
        "[a multiline\nlabel](https://example.com\n \"title\")\n\n"
        "a `multiline\ncode` span\n\n> > nested quote\n\n"
        "[reference][id]\n\n[id]: https://example.com\n"
        "\n[not a link] and [unresolved][missing]\n"
    )
    app = App(browser, url, startup=tricky, viewport={"width": 900, "height": 1200})
    app.page.wait_for_selector(".cm-md-h1")
    rendered = app.page.locator(".cm-content").inner_text()
    results.append(check(
        "nested, escaped, unfinished, setext and multiline syntax renders without changing text",
        "Setext heading" in rendered and "====" not in rendered and "***nested***" not in rendered
        and "\\*escaped\\*" in rendered and "**unfinished" in rendered
        and "[not a link]" in rendered and "[unresolved][missing]" in rendered
        and "https://example.com\n \"title\")" not in rendered
        and app.js("mock.text()") == tricky and not app.errors,
        [rendered, app.errors],
    ))
    app.page.close()

    # Many task widgets are a worst case for DOM allocation. Only the viewport
    # should be decorated, and scrolling must refresh both parsing and widgets.
    large = "Editing here\n\n" + "- [ ] task **bold**\n" * 12000
    app = App(browser, url, startup=large)
    app.page.wait_for_selector(".cm-md-checkbox")
    results.append(check(
        "large documents create only viewport-sized task widgets",
        1 < app.page.locator(".cm-md-checkbox").count() < 150,
        app.page.locator(".cm-md-checkbox").count(),
    ))
    app.js("mock.view().dispatch({selection: {anchor: mock.view().state.doc.length}, scrollIntoView: true})")
    app.page.wait_for_function(
        "document.querySelector('.cm-md-checkbox') && Number([...document.querySelectorAll('.cm-md-checkbox')].at(-1).dataset.from) > 200000"
    )
    results.append(check(
        "scrolling a large file renders the new viewport, not the entire file",
        app.page.locator(".cm-md-checkbox").count() < 150 and app.js("mock.text()") == large
        and not app.errors,
        app.errors,
    ))
    app.page.close()
    return results


BLOCK_SAMPLE = (
    "Editing here\n\n```javascript\nconst count = 42; // a comment\n"
    'console.log("hello");\n```\n\n'
    "| Name | Value |\n| :--- | ---: |\n| **bold** | `code` |\n"
    "| [site](https://example.com) | two |\n\n"
    "![Plot](images/plot.png)\n\nEnd\n"
)


def blocks(browser, url):
    results = []
    plain = App(browser, url, startup="Editing here\n\nNo code to highlight.\n")
    plain.page.wait_for_timeout(50)
    initial_scripts = set(plain.scripts)
    results.append(check(
        "plain text does not request image scope",
        "image_context" not in plain.js("mock.commands()"),
    ))
    plain.page.close()

    app = App(browser, url, startup=BLOCK_SAMPLE, viewport={"width": 1100, "height": 1200})
    app.page.wait_for_selector(".cm-code-keyword")
    app.page.wait_for_selector(".cm-md-table-preview table")
    app.page.wait_for_function("document.querySelector('.cm-md-image img')?.naturalWidth > 0")
    content = app.page.locator(".cm-content")
    results.append(check(
        "visible code fences load a language lazily and highlight tokens",
        "```" not in content.inner_text() and app.page.locator(".cm-code-keyword").inner_text() == "const"
        and app.page.locator(".cm-code-string").count() > 0
        and app.page.locator(".cm-code-comment").count() > 0 and set(app.scripts) > initial_scripts,
    ))
    results.append(check(
        "tables render semantic headers, alignment and inline markup",
        app.page.locator("thead th").all_text_contents() == ["Name", "Value"]
        and app.page.locator("tbody td strong").inner_text() == "bold"
        and app.page.locator("tbody td code").inner_text() == "code"
        and app.page.locator("tbody tr").first.locator("td").last.evaluate("(cell)=>getComputedStyle(cell).textAlign") == "right"
        and app.page.locator("table a[href]").count() == 0,
    ))
    results.append(check(
        "local images render through the narrow image protocol with alt text",
        app.page.locator(".cm-md-image img").get_attribute("alt") == "Plot"
        and app.js("mock.calls.filter(([cmd])=>cmd==='image_url').every(([,args])=>args.protocol==='markl-image')")
        and app.js("mock.text()") == BLOCK_SAMPLE and app.js("mock.edited()") == [],
    ))

    def select(pos, end=None):
        app.js(f"mock.view().dispatch({{selection: {{anchor: {pos}, head: {pos if end is None else end}}}, scrollIntoView: true}}); mock.view().focus()")
        app.page.wait_for_timeout(30)

    select(BLOCK_SAMPLE.index("count"))
    results.append(check(
        "editing a code block reveals both fences but keeps syntax highlighting",
        content.inner_text().count("```") == 2 and app.page.locator(".cm-code-keyword").count() > 0,
    ))
    select(0)
    app.page.locator("tbody td").first.click()
    results.append(check(
        "clicking a table cell reveals source at that cell, not at an unrelated position",
        app.page.locator("table").count() == 0 and "| **bold** | `code` |" in content.inner_text()
        and app.js("mock.view().state.selection.main.head") == BLOCK_SAMPLE.index("**bold**"),
    ))
    app.page.keyboard.type("New ")
    results.append(check(
        "editing a table cell changes only the original markdown",
        app.js("mock.text()") == BLOCK_SAMPLE.replace("**bold**", "New **bold**"),
    ))
    app.page.keyboard.press(f"{MOD}+z")
    select(0)
    app.page.wait_for_selector("table")
    app.js("mock.view().dispatch({changes: {from: 0, insert: 'prefix '}})")
    app.page.locator("tbody td").first.click()
    results.append(check(
        "table click positions remain correct after edits before the table",
        app.js("mock.view().state.selection.main.head") == len("prefix ") + BLOCK_SAMPLE.index("**bold**"),
    ))
    app.page.keyboard.press(f"{MOD}+z")
    select(0)
    app.page.wait_for_selector(".cm-md-table-preview")
    app.page.locator(".cm-md-table-preview").focus()
    app.page.locator(".cm-md-table-preview").press("Enter")
    results.append(check(
        "rendered tables support keyboard entry into source",
        app.page.locator("table").count() == 0
        and app.js("mock.view().state.selection.main.head") == BLOCK_SAMPLE.index("Name"),
    ))
    select(0)
    app.page.wait_for_function("document.querySelector('.cm-md-image img')?.naturalWidth > 0")
    before = app.page.locator(".cm-md-image img").get_attribute("src")
    app.command("save-as")
    app.page.wait_for_function(f"document.querySelector('.cm-md-image img')?.naturalWidth > 0 && document.querySelector('.cm-md-image img').src !== {json.dumps(before)}")
    results.append(check(
        "Save As invalidates the image context and refreshes relative image URLs",
        app.page.locator(".cm-md-image img").get_attribute("src") != before
        and app.js("mock.saved") == BLOCK_SAMPLE,
    ))
    select(BLOCK_SAMPLE.index("![Plot]") + 3)
    results.append(check(
        "editing an image reveals its markdown rather than changing the document",
        "![Plot](images/plot.png)" in content.inner_text() and app.page.locator(".cm-md-image").count() == 0
        and app.js("mock.text()") == BLOCK_SAMPLE,
    ))
    app.command("save")
    results.append(check(
        "code, table and image previews save byte-exact markdown without plugin errors",
        app.js("mock.saved") == BLOCK_SAMPLE and not app.errors,
        app.errors,
    ))
    app.page.close()

    app = App(browser, url)
    app.js("mock.imageDelay = 150; mock.nextOpen = 'Editing here\\n\\n![Old](images/plot.png)\\n'")
    app.command("open")
    app.command("new")
    app.page.wait_for_timeout(200)
    results.append(check(
        "late image-context replies cannot load an image into a replacement document",
        app.js("mock.text()") == "" and "image_url" not in app.js("mock.commands()") and not app.errors,
        app.errors,
    ))
    app.page.close()

    offscreen = "Editing here\n\n" + "A paragraph.\n\n" * 1500 + "```python\nanswer = 42\nprint('hello')\n```\n\nEnd\n"
    app = App(browser, url, startup=offscreen)
    app.page.wait_for_timeout(100)
    results.append(check(
        "offscreen code blocks do not download a language pack",
        set(app.scripts) == initial_scripts,
        app.scripts,
    ))
    app.js("mock.view().dispatch({selection: {anchor: mock.view().state.doc.length}, scrollIntoView: true})")
    app.page.wait_for_selector(".cm-code-string")
    results.append(check(
        "scrolling a code block into view loads and highlights it without a full-file preview",
        set(app.scripts) > initial_scripts and app.js("mock.text()") == offscreen and not app.errors,
        app.errors,
    ))
    app.page.close()

    snippets = {
        "typescript": "const count: number = 42;",
        "tsx": 'const view = <Panel title="hello"/>;',
        "json": '{"answer": 42, "ok": true}',
        "css": "body { color: red; }",
        "html": '<div title="hello">content</div>',
        "python": 'def greet():\n    return "hello"',
        "rust": 'fn main() { let value = "hello"; }',
    }
    mixed = "Editing here\n\n" + "\n\n".join(f"```{name}\n{code}\n```" for name, code in snippets.items()) + "\n"
    app = App(browser, url, startup=mixed, viewport={"width": 1100, "height": 2200})
    app.page.wait_for_function("""() => {
      const labels = [...document.querySelectorAll('.cm-md-code-label')];
      return labels.length === 7 && labels.every(label =>
        label.closest('.cm-line').nextElementSibling.querySelector('[class*="cm-code-"]'));
    }""")
    results.append(check(
        "TypeScript, TSX, JSON, CSS, HTML, Python and Rust packs all highlight without executing code",
        app.js("mock.text()") == mixed and not app.errors,
        app.errors,
    ))
    app.page.close()

    unsafe = "Editing here\n\n![Tracking](https://example.com/tracker.png)\n\n![Data](data:image/svg+xml,boom)\n\n![Absolute](/etc/passwd)\n\n"
    app = App(browser, url, startup=unsafe)
    results.append(check(
        "remote, data and absolute image URLs never make image or network requests",
        app.page.locator(".cm-md-image-status").count() == 3
        and all("unavailable" in value for value in app.page.locator(".cm-md-image-status").all_text_contents())
        and not any("example.com" in request for request in app.requests)
        and "image_context" not in app.js("mock.commands()") and app.js("mock.text()") == unsafe,
    ))
    app.page.close()

    large_table = "Editing here\n\n| Name | Value |\n| --- | --- |\n" + "| one | two |\n" * 150
    app = App(browser, url, startup=large_table)
    app.page.wait_for_timeout(50)
    results.append(check(
        "oversized tables retain source rather than allocate unbounded table widgets",
        app.page.locator("table").count() == 0 and app.js("mock.text()") == large_table and not app.errors,
    ))
    app.page.close()

    hostile = (
        "Editing here\n\n```__proto__\nunknown code\n```\n\n"
        "| Name | Text |\n| --- | --- |\n| a\\|b | <script>window.attacked=true</script> |\n"
    )
    app = App(browser, url, startup=hostile)
    app.page.wait_for_selector("table")
    results.append(check(
        "unknown language names, escaped pipes and HTML in table cells are handled as safe text",
        app.page.locator("tbody td").first.inner_text() == "a|b"
        and app.page.locator("table script").count() == 0 and app.js("window.attacked") is None
        and app.js("mock.text()") == hostile and not app.errors,
        [app.page.locator("table").inner_text(), app.errors],
    ))
    app.page.close()
    sparse = "Editing here\n\n| A | B | C |\n|---|---|---|\n| one || three |\n| | two | |\n| |\n"
    app = App(browser, url, startup=sparse)
    app.page.wait_for_selector("table")
    results.append(check(
        "empty table cells do not shift later values into the wrong column",
        app.page.locator("tbody tr").first.locator("td").all_text_contents() == ["one", "", "three"]
        and app.page.locator("tbody tr").nth(1).locator("td").all_text_contents() == ["", "two", ""]
        and app.page.locator("tbody tr").last.locator("td").all_text_contents() == ["", "", ""],
    ))
    app.page.locator("tbody tr").last.locator("td").last.click()
    results.append(check(
        "clicking an empty table cell produces a valid source position",
        app.js("Number.isFinite(mock.view().state.selection.main.head)") and not app.errors,
        app.errors,
    ))
    app.page.close()
    return results


def external_requests(browser, url):
    results = []
    app = App(browser, url, pending=[{"id": 1, "text": "Startup file"}, {"id": 2, "text": "Queued during startup"}])
    app.page.wait_for_function("mock.text() === 'Queued during startup' && document.querySelector('#editor').getAttribute('aria-busy') === 'false'")
    results.append(check(
        "startup and pre-ready native requests are opened without losing an event",
        app.js("mock.commands().filter(cmd => cmd === 'open_pending').length") == 1
        and app.js("mock.commands().includes('ask_save_changes')") is False and not app.errors,
        app.errors,
    ))
    app.page.close()

    def dirty_app(text="Original"):
        app = App(browser, url, startup=text)
        app.page.keyboard.press(f"{MOD}+End")
        app.page.keyboard.type(" unsaved")
        return app, app.js("mock.text()")

    for answer in ("cancel", "save", "discard"):
        app, original = dirty_app()
        app.js(f"mock.answer = {json.dumps(answer)}; mock.queueFile('External file')")
        app.page.wait_for_function("document.querySelector('#editor').getAttribute('aria-busy') === 'false' && (mock.text() === 'External file' || mock.commands().includes('cancel_pending_open'))")
        commands = app.js("mock.commands()")
        if answer == "cancel":
            ok = app.js("mock.text()") == original and "open_pending" not in commands and app.js("mock.pending.length") == 0
        elif answer == "save":
            ok = app.js("mock.saved") == original and app.js("mock.text()") == "External file" and commands.index("save") < commands.index("open_pending")
        else:
            ok = app.js("mock.text()") == "External file" and "save" not in commands
        results.append(check(f"external opens honour the {answer} choice for unsaved text", ok, commands))
        results.append(check(
            "native opens send only a request ID back to Rust, never a supplied path",
            app.js("mock.calls.filter(([cmd])=>cmd==='open_pending').every(([,args])=>Object.keys(args).join() === 'id')"),
        ))
        app.page.close()

    app, original = dirty_app()
    app.js("mock.answer = 'save'; mock.saveError = true; mock.queueFile('Must not open')")
    app.page.wait_for_function("mock.commands().includes('cancel_pending_open') && !mock.view().state.readOnly")
    results.append(check(
        "cancelled or failed save preserves the buffer and cancels the external open",
        app.js("mock.text()") == original and "open_pending" not in app.js("mock.commands()")
        and app.js("mock.edited().at(-1)") is True,
    ))
    app.page.close()

    app, original = dirty_app()
    app.js("mock.answer = 'discard'; mock.queueFile('Cannot read', true)")
    app.page.wait_for_function("mock.commands().includes('open_pending') && !mock.view().state.readOnly")
    results.append(check(
        "a failed external file read never discards the current buffer",
        app.js("mock.text()") == original and app.js("mock.edited().at(-1)") is True and not app.errors,
        app.errors,
    ))
    app.page.close()

    app, original = dirty_app("Editing here\n\n- [ ] Keep this task\n")
    app.js("mock.askDelay = 1000; mock.answer = 'discard'; mock.queueFile('First'); mock.queueFile('Second')")
    app.page.wait_for_function("mock.commands().includes('ask_save_changes') && mock.view().state.readOnly")
    app.page.keyboard.type("must not be inserted")
    app.page.locator(".cm-md-checkbox").press("Space")
    results.append(check(
        "typing and task-checkbox edits are blocked while a native document operation is pending",
        app.js("mock.text()") == original and not app.page.locator(".cm-md-checkbox").is_checked(),
    ))
    app.page.wait_for_function("mock.text() === 'Second' && !mock.view().state.readOnly")
    results.append(check(
        "rapid native requests are serialized and do not overlap unsaved-changes prompts",
        app.js("mock.commands().filter(cmd => cmd === 'ask_save_changes').length") == 1
        and app.js("mock.commands().filter(cmd => cmd === 'open_pending').length") == 2 and not app.errors,
        app.errors,
    ))
    app.page.close()

    app, original = dirty_app()
    app.js("mock.askDelay = 100; mock.answer = 'cancel'; mock.queueFile('First'); mock.queueFile('Second')")
    app.page.wait_for_function("mock.commands().includes('cancel_pending_open') && !mock.view().state.readOnly")
    results.append(check(
        "Cancel cancels the queued burst instead of repeatedly asking to replace the same text",
        app.js("mock.text()") == original and app.js("mock.pending.length") == 0
        and app.js("mock.commands().filter(cmd => cmd === 'ask_save_changes').length") == 1,
    ))
    app.page.close()

    app = App(browser, url, startup="Before")
    app.js("mock.readDelay = 150; mock.queueFile('After')")
    app.page.wait_for_function("mock.commands().includes('open_pending')")
    app.js("mock.emit('save')")
    app.page.wait_for_function("mock.saved === 'After' && !mock.view().state.readOnly")
    results.append(check(
        "a menu Save arriving during an external open saves the new buffer, not old text to a new path",
        app.js("mock.text()") == "After" and not app.errors,
        app.errors,
    ))
    app.page.close()

    app = App(browser, url)
    app.js("mock.onEmptyRequest = 'Late request'; mock.queueFile('First request')")
    app.page.wait_for_function("mock.text() === 'Late request' && !mock.view().state.readOnly")
    results.append(check("a request arriving while the queue drains is not lost", not app.errors, app.errors))
    app.page.close()

    app = App(browser, url, startup="Keep this text")
    app.js("mock.emitEvent('file-drag', true)")
    results.append(check("native file drags show a non-interactive drop hint", app.page.locator("#drop-target").is_visible()))
    prevented = app.js("""(() => {
      const dataTransfer = new DataTransfer();
      dataTransfer.items.add(new File(["untrusted contents"], "dropped.md", {type: "text/markdown"}));
      dataTransfer.setData("text/plain", "must not be inserted");
      const event = new DragEvent("drop", {dataTransfer, bubbles: true, cancelable: true});
      document.querySelector(".cm-content").dispatchEvent(event);
      return event.defaultPrevented;
    })()""")
    results.append(check(
        "browser file drops cannot navigate, insert file contents or bypass Rust's native queue",
        prevented and app.js("mock.text()") == "Keep this text"
        and "open_pending" not in app.js("mock.commands()") and not app.page.locator("#drop-target").is_visible(),
    ))
    app.js("mock.emitEvent('file-drag', true); mock.emitEvent('file-drag', false)")
    results.append(check(
        "leaving a native file drag removes the drop hint without editing text",
        not app.page.locator("#drop-target").is_visible() and app.js("mock.edited()") == [] and not app.errors,
        app.errors,
    ))
    app.page.close()
    return results


def run(browser, url, screenshots):
    results = []

    app = App(browser, url)
    results.append(check("starts empty, not edited", app.js("mock.text()") == "" and app.js("mock.edited()") == []))
    app.type("# Hello")
    results.append(check("typing marks the document edited once", app.js("mock.edited()") == [True]))
    for _ in range(3):
        app.page.keyboard.press(f"{MOD}+z")
    results.append(check(
        "undoing back to the saved text clears edited",
        app.js("mock.text()") == "" and app.js("mock.edited()") == [True, False],
        app.js("[mock.text(), mock.edited()]"),
    ))

    app.type("- one")
    app.page.keyboard.press("Enter")
    app.page.keyboard.type("two")
    results.append(check("Enter continues a list", app.js("mock.text()") == "- one\n- two", app.js("mock.text()")))

    app.js("mock.answer = 'cancel'; mock.nextOpen = 'other'")
    commands = app.command("open")
    results.append(check(
        "open with unsaved changes asks, and cancel keeps the text",
        commands == ["ask_save_changes"] and app.js("mock.text()") == "- one\n- two",
        commands,
    ))

    app.js("mock.answer = 'discard'; mock.nextOpen = 'line one\\r\\nline two\\r\\n'")
    commands = app.command("open")
    results.append(check(
        "discard opens the file, line breaks normalized, not edited",
        commands[:2] == ["ask_save_changes", "open_file"]
        and app.js("mock.text()") == "line one\nline two\n"
        and app.js("mock.edited().at(-1)") is False,
        [commands, app.js("mock.text()")],
    ))

    app.js("mock.nextOpen = null")
    commands = app.command("open")
    results.append(check(
        "cancelled open dialog changes nothing",
        commands == ["open_file", "recent_files"] and app.js("mock.text()") == "line one\nline two\n",
        commands,
    ))

    app.page.keyboard.press(f"{MOD}+End")
    app.page.keyboard.type("äöü 🙂\ttab")
    commands = app.command("save")
    results.append(check(
        "save sends exactly the editor text and clears edited",
        commands[:2] == ["save", "set_edited"]
        and app.js("mock.saved") == app.js("mock.text()") == "line one\nline two\näöü 🙂\ttab"
        and app.js("mock.edited().at(-1)") is False,
        [commands, app.js("mock.saved")],
    ))

    app.type("!")
    app.js("mock.answer = 'save'")
    commands = app.command("close")
    results.append(check(
        "close with unsaved changes: Save saves, then quits",
        commands[:2] == ["ask_save_changes", "save"] and commands[-1] == "quit",
        commands,
    ))

    app.type("?")
    app.js("mock.answer = 'cancel'")
    commands = app.command("close")
    results.append(check("close, Cancel: doesn't quit", "quit" not in commands, commands))

    app.js("mock.answer = 'discard'")
    commands = app.command("new")
    results.append(check(
        "new after Don't Save: empty, untitled",
        "new_file" in commands and app.js("mock.text()") == "",
        commands,
    ))

    tricky = "No final newline\n\n  indented  \n\t\ttabs\n> quote\n```\ncode\n```\n|a|b|\n🙂 é"
    app.js(f"mock.answer = 'discard'; mock.nextOpen = {tricky!r}")
    app.command("open")
    app.command("save")
    results.append(check("open then save is byte-exact", app.js("mock.saved") == tricky, app.js("mock.saved")))

    results += sidebar(browser, url)
    results += preview(browser, url)
    results += blocks(browser, url)
    results += external_requests(browser, url)

    if screenshots:
        screenshots.mkdir(parents=True, exist_ok=True)
        recent = [{**RECENT[0], "name": "preview.md", "dir": "~/Notes"}, *RECENT[1:3]]
        for scheme in ("light", "dark"):
            for name, sample in (("editor", PREVIEW_SAMPLE), ("blocks", BLOCK_SAMPLE)):
                shot = App(browser, url, startup=sample, recent=recent,
                           viewport={"width": 1100, "height": 720}, color_scheme=scheme)
                shot.page.wait_for_selector("#sidebar li")
                if name == "blocks":
                    shot.page.wait_for_selector("table")
                    shot.page.wait_for_selector(".cm-code-keyword")
                    shot.page.wait_for_function("document.querySelector('.cm-md-image img')?.naturalWidth > 0")
                shot.recent("plan.md").hover()
                shot.page.screenshot(path=str(screenshots / f"{name}-{scheme}.png"))
                shot.page.close()
        print(f"     screenshots in {screenshots}")

    if FIXTURES.exists():
        def summary(latencies):
            return f"median {statistics.median(latencies):.1f} ms, worst {max(latencies):.1f} ms"

        small = typing_latency(browser, url, (ROOT / "README.md").read_text())
        medium = typing_latency(browser, url, (FIXTURES / "1m.md").read_text())
        large = typing_latency(browser, url, (FIXTURES / "10m.md").read_text())
        print(f"     keypress to frame at the end of a file (Chromium, 60 Hz): small {summary(small)}")
        results.append(check(f"1 MB file: {summary(medium)}, under 16 ms", statistics.median(medium) < 16))
        # Not a budget: @lezer/markdown rebuilds the document's list of top-level blocks on every
        # re-parse, which takes ~12 ms per edit near the end of a 10 MB file.
        print(f"info 10 MB file: {summary(large)}")
    else:
        print("skip typing latency: run `bun run fixtures` first")

    return all(results)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--screenshots", type=Path, help="save light and dark screenshots here")
    parser.add_argument("--webkit-preview", action="store_true", help="run only inline-preview checks in WebKit")
    args = parser.parse_args()
    if not (DIST / "index.html").exists():
        raise SystemExit("dist/ not found; run `bun run build` first")

    server = serve(DIST)
    try:
        with sync_playwright() as playwright:
            browser = (playwright.webkit if args.webkit_preview else playwright.chromium).launch()
            url = f"http://127.0.0.1:{server.server_address[1]}/"
            ok = all(preview(browser, url) + blocks(browser, url) + external_requests(browser, url)) if args.webkit_preview else run(browser, url, args.screenshots)
            browser.close()
    finally:
        server.shutdown()
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()

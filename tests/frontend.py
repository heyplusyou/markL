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
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
FIXTURES = ROOT / "bench/fixtures"
MOD = "Meta" if sys.platform == "darwin" else "Control"

# Stands in for Tauri's IPC bridge. Records every command; tests set what the "Rust side" answers.
MOCK = """
window.mock = { calls: [], handlers: {}, answer: "cancel", nextOpen: null, startup: null, saved: null, recent: [], files: {} };
let nextId = 1;
window.__TAURI_INTERNALS__ = {
  metadata: { currentWindow: { label: "main" }, currentWebview: { windowLabel: "main", label: "main" } },
  transformCallback(callback) { const id = nextId++; mock.handlers[id] = callback; return id; },
  unregisterCallback(id) { delete mock.handlers[id]; },
  async invoke(cmd, args) {
    const raw = args instanceof Uint8Array;
    mock.calls.push([cmd, raw ? new TextDecoder().decode(args) : args]);
    const bytes = (text) => new TextEncoder().encode(text).buffer;
    switch (cmd) {
      case "plugin:event|listen": mock.listener = args.handler; return 1;
      case "open_startup_file": if (mock.startup == null) throw "no file"; return bytes(mock.startup);
      case "open_file": if (mock.nextOpen == null) throw "cancelled"; return bytes(mock.nextOpen);
      case "save": case "save_as": mock.saved = new TextDecoder().decode(args); return null;
      case "ask_save_changes": return mock.answer;
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


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


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
    def __init__(self, browser, url, startup=None, recent=(), files=None, **page_options):
        self.page = browser.new_page(**page_options)
        self.page.add_init_script(MOCK)
        self.page.add_init_script(f"mock.recent = {json.dumps(list(recent))}; mock.files = {json.dumps(files or {})};")
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

    if screenshots:
        screenshots.mkdir(parents=True, exist_ok=True)
        sample = (ROOT / "README.md").read_text()
        recent = [{**RECENT[0], "name": "README.md", "dir": "~/Development/markL"}, *RECENT[1:3]]
        for scheme in ("light", "dark"):
            shot = App(browser, url, startup=sample, recent=recent,
                       viewport={"width": 1100, "height": 720}, color_scheme=scheme)
            shot.page.wait_for_selector("#sidebar li")
            shot.recent("plan.md").hover()
            shot.page.screenshot(path=str(screenshots / f"editor-{scheme}.png"))
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
    args = parser.parse_args()
    if not (DIST / "index.html").exists():
        raise SystemExit("dist/ not found; run `bun run build` first")

    server = serve(DIST)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            ok = run(browser, f"http://127.0.0.1:{server.server_address[1]}/", args.screenshots)
            browser.close()
    finally:
        server.shutdown()
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()

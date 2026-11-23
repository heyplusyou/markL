import { invoke, type InvokeArgs } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { type Text } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { createState, setEditingLocked } from "./editor";
import { refreshImages } from "./images";
import { createSidebar, type RecentFile } from "./sidebar";

// The document's text as last opened or saved, to tell whether there are unsaved changes.
let saved: Text;
let edited = false;
let busy = false;
let finishStartup!: () => void;
let work: Promise<unknown> = new Promise<void>((resolve) => { finishStartup = resolve; });

const view = new EditorView({ parent: document.getElementById("editor")! });
load("");
setBusy(true);

const showRecent = createSidebar(document.getElementById("sidebar")!, {
  open: (file) => file.current || serial(() => open("open_recent", { path: file.path })),
  remove: (file) => invoke("remove_recent", { path: file.path }).then(refreshRecent),
});

function setBusy(next: boolean) {
  busy = next;
  setEditingLocked(view, next);
  document.getElementById("editor")!.setAttribute("aria-busy", String(next));
  if (!next) view.focus();
}

// A modal dialog or asynchronous file read must not race another open/save or
// edits to the old buffer. Keep internals unqueued so Save from a prompt works.
function serial<T>(action: () => Promise<T>): Promise<T> {
  const task = work.then(async () => {
    setBusy(true);
    try { return await action(); }
    finally { setBusy(false); }
  });
  work = task.catch(() => {});
  return task;
}

function load(text: string) {
  view.setState(createState(text, changed));
  if (busy) setEditingLocked(view, true);
  saved = view.state.doc;
  changed(saved);
  view.focus();
}

function changed(doc: Text) {
  const now = !doc.eq(saved);
  if (now !== edited) {
    edited = now;
    invoke("set_edited", { edited });
  }
}

// Rust sends file text as raw bytes; it has checked that they are UTF-8.
function decode(bytes: ArrayBuffer): string {
  return new TextDecoder().decode(bytes);
}

async function refreshRecent() {
  showRecent(await invoke<RecentFile[]>("recent_files"));
}

async function save(as: boolean): Promise<boolean> {
  const doc = view.state.doc;
  try {
    await invoke(as ? "save_as" : "save", new TextEncoder().encode(doc.toString()));
  } catch {
    return false; // cancelled, or failed and Rust has told the user
  }
  saved = doc;
  changed(view.state.doc);
  refreshImages(view);
  refreshRecent();
  return true;
}

// Before the document is replaced or closed: true if it's fine to go ahead.
async function confirmDiscard(): Promise<boolean> {
  if (!edited) return true;
  const choice = await invoke<"save" | "discard" | "cancel">("ask_save_changes");
  return choice === "discard" || (choice === "save" && (await save(false)));
}

// Replaces the document with a file that a Rust command opens, after asking about unsaved changes.
async function open(command: string, args?: InvokeArgs) {
  if (!(await confirmDiscard())) return;
  const bytes = await invoke<ArrayBuffer>(command, args).catch(() => null);
  if (bytes) load(decode(bytes));
  refreshRecent();
}

const commands: Record<string, () => Promise<unknown>> = {
  async new() {
    if (!(await confirmDiscard())) return;
    await invoke("new_file");
    load("");
    refreshRecent();
  },
  open: () => open("open_file"),
  save: () => save(false),
  "save-as": () => save(true),
  async close() {
    if (await confirmDiscard()) await invoke("quit");
  },
};

let externalScheduled = false;
let externalAgain = false;

function externalOpen() {
  externalAgain = true;
  if (externalScheduled) return;
  externalScheduled = true;
  const task = serial(async () => {
    do {
      externalAgain = false;
      for (;;) {
        const id = await invoke<number | null>("next_open_request");
        if (id == null) break;
        if (!(await confirmDiscard())) {
          externalAgain = false;
          await invoke("cancel_pending_open", { id });
          return;
        }
        const bytes = await invoke<ArrayBuffer>("open_pending", { id }).catch(() => null);
        if (bytes) load(decode(bytes));
        refreshRecent();
      }
    } while (externalAgain);
  });
  const finish = () => {
    externalScheduled = false;
    if (externalAgain) externalOpen();
  };
  void task.then(finish, finish);
}

const dropTarget = document.createElement("div");
dropTarget.id = "drop-target";
dropTarget.hidden = true;
dropTarget.setAttribute("role", "status");
dropTarget.setAttribute("aria-live", "polite");
dropTarget.textContent = "Drop a file to open";
document.body.append(dropTarget);

// Native Tauri drop events own file paths. Browser file drops must neither
// navigate the webview nor insert a filename/HTML into the current document.
for (const name of ["dragover", "drop"]) {
  document.addEventListener(name, (event) => {
    if ((event as DragEvent).dataTransfer?.types.includes("Files")) {
      event.preventDefault();
      event.stopPropagation();
      if (name === "drop") dropTarget.hidden = true;
    }
  }, true);
}
window.addEventListener("blur", () => { dropTarget.hidden = true; });
// Files may have been moved or deleted while markL was in the background.
window.addEventListener("focus", refreshRecent);

try {
  await Promise.all([
    listen<string>("command", (event) => {
      if (Object.prototype.hasOwnProperty.call(commands, event.payload)) void serial(commands[event.payload]);
    }),
    listen("open-request", externalOpen),
    listen<boolean>("file-drag", (event) => { dropTarget.hidden = event.payload !== true; }),
  ]);
  const startup = await invoke<ArrayBuffer>("open_startup_file").catch(() => null);
  if (startup) load(decode(startup));
} finally {
  setBusy(false);
  finishStartup();
}
await invoke("ready");
refreshRecent();

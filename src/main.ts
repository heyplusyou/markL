import { invoke, type InvokeArgs } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { type Text } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { createState } from "./editor";
import { createSidebar, type RecentFile } from "./sidebar";

// The document's text as last opened or saved, to tell whether there are unsaved changes.
let saved: Text;
let edited = false;

const view = new EditorView({ parent: document.getElementById("editor")! });
load("");

const showRecent = createSidebar(document.getElementById("sidebar")!, {
  open: (file) => file.current || open("open_recent", { path: file.path }),
  remove: (file) => invoke("remove_recent", { path: file.path }).then(refreshRecent),
});

function load(text: string) {
  view.setState(createState(text, changed));
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

listen<string>("command", (event) => commands[event.payload]?.());
// Files may have been moved or deleted while markL was in the background.
window.addEventListener("focus", refreshRecent);

const startup = await invoke<ArrayBuffer>("open_startup_file").catch(() => null);
if (startup) load(decode(startup));
invoke("ready");
refreshRecent();

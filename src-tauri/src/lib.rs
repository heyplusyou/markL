mod document;
mod menu;
mod recent;

use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard, OnceLock, PoisonError, mpsc};
use std::time::{SystemTime, UNIX_EPOCH};

use rfd::{FileDialog, MessageButtons, MessageDialog, MessageDialogResult, MessageLevel};
use serde::Serialize;
use tauri::ipc::{InvokeBody, Request, Response};
use tauri::window::Color;
use tauri::{
    AppHandle, Emitter, Manager, Runtime, State, Theme, WebviewWindow, WebviewWindowBuilder,
    WindowEvent,
};

use document::Format;
use recent::Recent;

/// When `main` started and when the window was shown, in ms since the epoch. Printed with
/// `MARKL_TIMING=1` for startup measurements.
static STARTED_MS: OnceLock<u128> = OnceLock::new();
static SHOWN_MS: OnceLock<u128> = OnceLock::new();

const EXTENSIONS: &[&str] = &["md", "markdown", "mdown", "mkd", "txt"];
/// The error a command returns when the user cancels a dialog; the frontend ignores it.
const CANCELLED: &str = "cancelled";
const SAVE: &str = "Save";
const DONT_SAVE: &str = "Don't Save";
const CANCEL: &str = "Cancel";

/// The open document. The editor holds its text; Rust holds where it lives and how it is stored.
#[derive(Default)]
struct Document {
    path: Option<PathBuf>,
    format: Format,
    edited: bool,
}

type Doc = Mutex<Document>;
type RecentList = Mutex<Recent>;

/// A recent file as the sidebar shows it.
#[derive(Serialize)]
struct RecentFile {
    path: String,
    name: String,
    dir: String,
    exists: bool,
    current: bool,
}

impl Document {
    fn name(&self) -> String {
        self.path
            .as_deref()
            .map_or_else(|| "Untitled".to_string(), file_name)
    }

    fn dir(&self) -> Option<PathBuf> {
        self.path
            .as_deref()
            .and_then(Path::parent)
            .map(Path::to_path_buf)
    }

    fn title(&self) -> String {
        if self.edited {
            format!("{} — Edited", self.name())
        } else {
            self.name()
        }
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

fn is_edited<R: Runtime>(app: &AppHandle<R>) -> bool {
    lock(&app.state::<Doc>()).edited
}

fn file_name(path: &Path) -> String {
    path.file_name().map_or_else(
        || path.display().to_string(),
        |name| name.to_string_lossy().into_owned(),
    )
}

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis())
}

/// The page background (`--bg` in styles.css), painted before the page has loaded.
fn background(theme: Theme) -> Color {
    match theme {
        Theme::Dark => Color(0x1e, 0x1e, 0x20, 0xff),
        _ => Color(0xff, 0xff, 0xff, 0xff),
    }
}

/// The file named on the command line (`markl notes.md`), if any.
fn startup_path() -> Option<PathBuf> {
    let arg = std::env::args_os()
        .skip(1)
        .find(|arg| !arg.to_string_lossy().starts_with('-'))?;
    std::path::absolute(arg).ok()
}

/// Runs `dialog` on the main thread, where macOS needs native dialogs, and waits for it without
/// blocking the command's thread. `None` if the app is shutting down.
async fn on_main<T: Send + 'static>(
    window: &WebviewWindow,
    dialog: impl FnOnce(&WebviewWindow) -> T + Send + 'static,
) -> Option<T> {
    let (sender, receiver) = mpsc::channel();
    let parent = window.clone();
    window
        .run_on_main_thread(move || {
            let _ = sender.send(dialog(&parent));
        })
        .ok()?;
    tauri::async_runtime::spawn_blocking(move || receiver.recv().ok())
        .await
        .ok()
        .flatten()
}

async fn alert(window: &WebviewWindow, message: String, detail: String) {
    on_main(window, move |parent| {
        MessageDialog::new()
            .set_parent(parent)
            .set_level(MessageLevel::Error)
            .set_title(message)
            .set_description(detail)
            .set_buttons(MessageButtons::Ok)
            .show()
    })
    .await;
}

/// Makes `path` the document and returns its text.
async fn open_path(
    window: &WebviewWindow,
    doc: &Doc,
    recent: &RecentList,
    path: PathBuf,
) -> Result<Response, String> {
    let (text, format) = match document::read(&path) {
        Ok(read) => read,
        Err(err) => {
            let message = format!("“{}” can't be opened.", file_name(&path));
            alert(window, message.clone(), err.to_string()).await;
            return Err(message);
        }
    };
    lock(recent).add(&path);
    let mut doc = lock(doc);
    *doc = Document {
        path: Some(path),
        format,
        edited: false,
    };
    let _ = window.set_title(&doc.title());
    Ok(Response::new(text))
}

async fn choose_save_path(window: &WebviewWindow, doc: &Doc) -> Option<PathBuf> {
    let (name, dir) = {
        let doc = lock(doc);
        let name = match doc.path {
            Some(_) => doc.name(),
            None => "Untitled.md".to_string(),
        };
        (name, doc.dir())
    };
    on_main(window, move |parent| {
        let dialog = FileDialog::new()
            .set_parent(parent)
            .add_filter("Markdown", EXTENSIONS)
            .set_file_name(name);
        match dir {
            Some(dir) => dialog.set_directory(dir),
            None => dialog,
        }
        .save_file()
    })
    .await
    .flatten()
}

async fn write(
    window: &WebviewWindow,
    doc: &Doc,
    recent: &RecentList,
    path: PathBuf,
    text: &[u8],
) -> Result<(), String> {
    let format = lock(doc).format;
    if let Err(err) = document::write_atomically(&path, &document::encode(text, format)) {
        let message = format!("“{}” couldn't be saved.", file_name(&path));
        alert(window, message.clone(), err.to_string()).await;
        return Err(message);
    }
    lock(recent).add(&path);
    let mut doc = lock(doc);
    doc.path = Some(path);
    let _ = window.set_title(&doc.title());
    Ok(())
}

/// The editor text sent with a save command: raw UTF-8 bytes, not JSON.
fn text_of(request: &Request<'_>) -> Result<Vec<u8>, String> {
    match request.body() {
        InvokeBody::Raw(bytes) => Ok(bytes.clone()),
        InvokeBody::Json(_) => Err("expected the document text as raw bytes".into()),
    }
}

/// Called by the frontend once the editor is ready to type in.
#[tauri::command]
fn ready() {
    if std::env::var_os("MARKL_TIMING").is_some() {
        let started = STARTED_MS.get().copied().unwrap_or_default();
        let shown = SHOWN_MS.get().copied().unwrap_or_default();
        eprintln!(
            "markl-timing main={started} shown={shown} ready={}",
            now_ms()
        );
    }
}

#[tauri::command]
async fn open_startup_file(
    window: WebviewWindow,
    doc: State<'_, Doc>,
    recent: State<'_, RecentList>,
) -> Result<Response, String> {
    let path = startup_path().ok_or("no file on the command line")?;
    open_path(&window, &doc, &recent, path).await
}

#[tauri::command]
async fn open_file(
    window: WebviewWindow,
    doc: State<'_, Doc>,
    recent: State<'_, RecentList>,
) -> Result<Response, String> {
    let dir = lock(&doc).dir();
    let path = on_main(&window, move |parent| {
        let dialog = FileDialog::new()
            .set_parent(parent)
            .add_filter("Markdown", EXTENSIONS);
        match dir {
            Some(dir) => dialog.set_directory(dir),
            None => dialog,
        }
        .pick_file()
    })
    .await
    .flatten()
    .ok_or(CANCELLED)?;
    open_path(&window, &doc, &recent, path).await
}

/// Opens a file from the recent list. Only paths on the list are accepted.
#[tauri::command]
async fn open_recent(
    window: WebviewWindow,
    doc: State<'_, Doc>,
    recent: State<'_, RecentList>,
    path: String,
) -> Result<Response, String> {
    let path = PathBuf::from(path);
    if !lock(&recent).contains(&path) {
        return Err("not a recent file".into());
    }
    open_path(&window, &doc, &recent, path).await
}

/// The recent files for the sidebar. Checks that each still exists, so it runs off the main thread.
#[tauri::command]
async fn recent_files(
    app: AppHandle,
    doc: State<'_, Doc>,
    recent: State<'_, RecentList>,
) -> Result<Vec<RecentFile>, String> {
    let current = lock(&doc).path.clone();
    let files = lock(&recent).files().to_vec();
    let home = app.path().home_dir().ok();
    Ok(files
        .iter()
        .map(|path| RecentFile {
            path: path.to_string_lossy().into_owned(),
            name: file_name(path),
            dir: path
                .parent()
                .map(|dir| recent::display_dir(dir, home.as_deref()))
                .unwrap_or_default(),
            exists: path.is_file(),
            current: current.as_deref() == Some(path.as_path()),
        })
        .collect())
}

#[tauri::command]
async fn remove_recent(recent: State<'_, RecentList>, path: String) -> Result<(), String> {
    lock(&recent).remove(Path::new(&path));
    Ok(())
}

#[tauri::command]
async fn save(
    window: WebviewWindow,
    doc: State<'_, Doc>,
    recent: State<'_, RecentList>,
    request: Request<'_>,
) -> Result<(), String> {
    let text = text_of(&request)?;
    let path = lock(&doc).path.clone();
    let path = match path {
        Some(path) => path,
        None => choose_save_path(&window, &doc).await.ok_or(CANCELLED)?,
    };
    write(&window, &doc, &recent, path, &text).await
}

#[tauri::command]
async fn save_as(
    window: WebviewWindow,
    doc: State<'_, Doc>,
    recent: State<'_, RecentList>,
    request: Request<'_>,
) -> Result<(), String> {
    let text = text_of(&request)?;
    let path = choose_save_path(&window, &doc).await.ok_or(CANCELLED)?;
    write(&window, &doc, &recent, path, &text).await
}

#[tauri::command]
fn new_file(window: WebviewWindow, doc: State<'_, Doc>) {
    let mut doc = lock(&doc);
    *doc = Document::default();
    let _ = window.set_title(&doc.title());
}

#[tauri::command]
fn set_edited(window: WebviewWindow, doc: State<'_, Doc>, edited: bool) {
    let mut doc = lock(&doc);
    doc.edited = edited;
    let _ = window.set_title(&doc.title());
}

/// Asks what to do with unsaved changes: "save", "discard" or "cancel".
#[tauri::command]
async fn ask_save_changes(
    window: WebviewWindow,
    doc: State<'_, Doc>,
) -> Result<&'static str, String> {
    let name = lock(&doc).name();
    let choice = on_main(&window, move |parent| {
        MessageDialog::new()
            .set_parent(parent)
            .set_level(MessageLevel::Warning)
            .set_title(format!(
                "Do you want to save the changes you made to “{name}”?"
            ))
            .set_description("Your changes will be lost if you don't save them.")
            .set_buttons(MessageButtons::YesNoCancelCustom(
                SAVE.into(),
                DONT_SAVE.into(),
                CANCEL.into(),
            ))
            .show()
    })
    .await;
    Ok(match choice {
        Some(MessageDialogResult::Custom(button)) if button == SAVE => "save",
        Some(MessageDialogResult::Custom(button)) if button == DONT_SAVE => "discard",
        Some(MessageDialogResult::Yes) => "save",
        Some(MessageDialogResult::No) => "discard",
        _ => "cancel",
    })
}

#[tauri::command]
fn quit(app: AppHandle) {
    app.exit(0);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    STARTED_MS.get_or_init(now_ms);
    tauri::Builder::default()
        .manage(Doc::default())
        .menu(menu::build)
        // Closing or quitting with unsaved changes goes through the frontend, which asks first.
        .on_menu_event(|app, event| match event.id().as_ref() {
            "quit" if !is_edited(app) => app.exit(0),
            "quit" => {
                let _ = app.emit("command", "close");
            }
            id => {
                let _ = app.emit("command", id);
            }
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event
                && is_edited(window.app_handle())
            {
                api.prevent_close();
                let _ = window.emit("command", "close");
            }
        })
        .setup(|app| {
            let store = app.path().config_dir()?.join("markL").join("recent.json");
            app.manage(RecentList::new(Recent::load(store)));

            // The window is created here, hidden, instead of from the config, so it can be painted
            // in the page's background colour and shown before the page has loaded: WebKit loads
            // pages in hidden windows more slowly, and an empty window must not flash white in dark
            // mode. Incognito: markL keeps no browser storage, and the web data store costs memory.
            let config = &app.config().app.windows[0];
            let window = WebviewWindowBuilder::from_config(app, config)?
                .incognito(true)
                .build()?;
            window.set_background_color(Some(background(window.theme()?)))?;
            window.set_title(&lock(&app.state::<Doc>()).title())?;
            window.show()?;
            SHOWN_MS.get_or_init(now_ms);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            ready,
            open_startup_file,
            open_file,
            open_recent,
            recent_files,
            remove_recent,
            save,
            save_as,
            new_file,
            set_edited,
            ask_save_changes,
            quit,
        ])
        .run(tauri::generate_context!())
        .expect("error while running markL");
}

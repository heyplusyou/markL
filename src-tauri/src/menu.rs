//! The app menu. markL's own items send their id to the frontend as a `command` event (see lib.rs);
//! the rest are native items.

use tauri::menu::{Menu, MenuBuilder, MenuItem, SubmenuBuilder};
use tauri::{AppHandle, Runtime};

pub fn build<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let item = |id: &str, text: &str, accelerator: &str| {
        MenuItem::with_id(app, id, text, true, Some(accelerator))
    };
    let quit = item(
        "quit",
        if cfg!(target_os = "macos") {
            "Quit markL"
        } else {
            "Quit"
        },
        "CmdOrCtrl+Q",
    )?;

    let file = SubmenuBuilder::new(app, "File")
        .item(&item("new", "New", "CmdOrCtrl+N")?)
        .item(&item("open", "Open…", "CmdOrCtrl+O")?)
        .separator()
        .item(&item("save", "Save", "CmdOrCtrl+S")?)
        .item(&item("save-as", "Save As…", "CmdOrCtrl+Shift+S")?)
        .separator()
        .close_window();
    // Quit is in the app menu on macOS and in the File menu elsewhere.
    #[cfg(not(target_os = "macos"))]
    let file = file.separator().item(&quit);

    let edit = SubmenuBuilder::new(app, "Edit")
        .undo()
        .redo()
        .separator()
        .cut()
        .copy()
        .paste()
        .select_all()
        .build()?;

    let menu = MenuBuilder::new(app);
    #[cfg(target_os = "macos")]
    let menu = menu.item(
        &SubmenuBuilder::new(app, "markL")
            .about(None)
            .separator()
            .services()
            .separator()
            .hide()
            .hide_others()
            .show_all()
            .separator()
            .item(&quit)
            .build()?,
    );
    let menu = menu.item(&file.build()?).item(&edit);
    #[cfg(target_os = "macos")]
    let menu = menu.item(
        &SubmenuBuilder::new(app, "Window")
            .minimize()
            .maximize()
            .build()?,
    );
    menu.build()
}

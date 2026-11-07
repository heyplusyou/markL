//! The recent-files list: paths only, most recent first, kept as JSON in markL's config directory.

use std::fs;
use std::path::{Component, Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::document;

const MAX: usize = 20;

#[derive(Default)]
pub struct Recent {
    files: Vec<PathBuf>,
    /// Where the list is saved; `None` keeps it in memory only.
    store: Option<PathBuf>,
}

/// The JSON file. Paths that aren't valid UTF-8 are left out.
#[derive(Default, Serialize, Deserialize)]
struct Stored {
    files: Vec<String>,
}

impl Recent {
    /// Loads the list from `store`. A missing or unreadable file gives an empty list.
    pub fn load(store: PathBuf) -> Self {
        let stored: Stored = fs::read(&store)
            .ok()
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
            .unwrap_or_default();
        Recent {
            files: stored.files.into_iter().map(PathBuf::from).collect(),
            store: Some(store),
        }
    }

    pub fn files(&self) -> &[PathBuf] {
        &self.files
    }

    pub fn contains(&self, path: &Path) -> bool {
        self.files.iter().any(|file| file == path)
    }

    /// Moves `path` to the top of the list, adding it if it's new.
    pub fn add(&mut self, path: &Path) {
        if self.files.first().is_some_and(|first| first == path) || path.to_str().is_none() {
            return;
        }
        self.files.retain(|file| file != path);
        self.files.insert(0, path.to_path_buf());
        self.files.truncate(MAX);
        self.save();
    }

    pub fn remove(&mut self, path: &Path) {
        let before = self.files.len();
        self.files.retain(|file| file != path);
        if self.files.len() != before {
            self.save();
        }
    }

    /// Writes the list. Failing to is not worth bothering the user about: the list is a convenience.
    fn save(&self) {
        let Some(store) = &self.store else { return };
        let stored = Stored {
            files: self
                .files
                .iter()
                .filter_map(|file| file.to_str().map(str::to_string))
                .collect(),
        };
        let written = serde_json::to_vec_pretty(&stored)
            .map_err(std::io::Error::other)
            .and_then(|json| {
                if let Some(dir) = store.parent() {
                    fs::create_dir_all(dir)?;
                }
                document::write_atomically(store, &json)
            });
        if let Err(err) = written {
            eprintln!("markl: can't save {}: {err}", store.display());
        }
    }
}

/// A folder as shown in the sidebar: the home directory as `~`, and only the last two folders of a
/// deep path, so that the part that tells files apart stays visible.
pub fn display_dir(dir: &Path, home: Option<&Path>) -> String {
    let (prefix, rest) = match home.and_then(|home| dir.strip_prefix(home).ok()) {
        Some(rest) => ("~", rest),
        None => ("", dir),
    };
    let names: Vec<_> = rest
        .components()
        .filter_map(|component| match component {
            Component::Normal(name) => Some(name.to_string_lossy()),
            _ => None,
        })
        .collect();
    let separator = std::path::MAIN_SEPARATOR_STR;
    if names.len() > 2 {
        format!("…{separator}{}", names[names.len() - 2..].join(separator))
    } else if prefix.is_empty() {
        dir.display().to_string()
    } else if names.is_empty() {
        prefix.to_string()
    } else {
        format!("{prefix}{separator}{}", names.join(separator))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn paths(recent: &Recent) -> Vec<&str> {
        recent.files().iter().map(|p| p.to_str().unwrap()).collect()
    }

    #[test]
    fn add_moves_to_the_top_and_keeps_twenty() {
        let mut recent = Recent::default();
        for name in ["/a.md", "/b.md", "/c.md", "/a.md"] {
            recent.add(Path::new(name));
        }
        assert_eq!(paths(&recent), ["/a.md", "/c.md", "/b.md"]);

        for i in 0..30 {
            recent.add(&PathBuf::from(format!("/{i}.md")));
        }
        assert_eq!(recent.files().len(), MAX);
        assert_eq!(paths(&recent)[0], "/29.md");
    }

    #[test]
    fn remove_and_contains() {
        let mut recent = Recent::default();
        recent.add(Path::new("/a.md"));
        recent.add(Path::new("/b.md"));
        recent.remove(Path::new("/a.md"));
        assert!(!recent.contains(Path::new("/a.md")));
        assert!(recent.contains(Path::new("/b.md")));
    }

    #[test]
    fn saves_and_loads() {
        let dir = std::env::temp_dir().join(format!("markl-recent-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let store = dir.join("nested/recent.json");

        let mut recent = Recent::load(store.clone());
        assert!(recent.files().is_empty());
        recent.add(Path::new("/x/one.md"));
        recent.add(Path::new("/y/two.md"));

        assert_eq!(
            paths(&Recent::load(store.clone())),
            ["/y/two.md", "/x/one.md"]
        );

        fs::write(&store, "not json").unwrap();
        assert!(Recent::load(store).files().is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn display_dir_shortens() {
        let home = Some(Path::new("/Users/me"));
        assert_eq!(display_dir(Path::new("/Users/me"), home), "~");
        assert_eq!(display_dir(Path::new("/Users/me/Notes"), home), "~/Notes");
        assert_eq!(
            display_dir(Path::new("/Users/me/Dev/markL"), home),
            "~/Dev/markL"
        );
        assert_eq!(display_dir(Path::new("/Users/me/a/b/c"), home), "…/b/c");
        assert_eq!(display_dir(Path::new("/tmp"), home), "/tmp");
        assert_eq!(
            display_dir(Path::new("/Volumes/Work"), home),
            "/Volumes/Work"
        );
        assert_eq!(display_dir(Path::new("/Volumes/Work/x/y"), home), "…/x/y");
    }
}

//! Native file requests. Paths never make a round trip through the webview.

use std::collections::VecDeque;
use std::ffi::OsString;
use std::path::{Path, PathBuf};

const MAX_PENDING: usize = 32;

#[derive(Default)]
pub struct Pending {
    requests: VecDeque<(u32, PathBuf)>,
    next_id: u32,
    pub ready: bool,
}

impl Pending {
    pub fn push(&mut self, path: PathBuf) -> bool {
        if self.requests.len() >= MAX_PENDING
            || self.requests.back().is_some_and(|(_, last)| last == &path)
        {
            return false;
        }
        self.next_id = self.next_id.wrapping_add(1).max(1);
        self.requests.push_back((self.next_id, path));
        true
    }

    pub fn has_requests(&self) -> bool {
        !self.requests.is_empty()
    }

    pub fn next(&mut self, current: Option<&Path>) -> Option<u32> {
        // Reopening the same file means "show this document", not "discard my edits".
        while self
            .requests
            .front()
            .is_some_and(|(_, path)| Some(path.as_path()) == current)
        {
            self.requests.pop_front();
        }
        self.requests.front().map(|(id, _)| *id)
    }

    pub fn take(&mut self, id: u32) -> Option<PathBuf> {
        if self.requests.front()?.0 != id {
            return None;
        }
        self.requests.pop_front().map(|(_, path)| path)
    }

    pub fn cancel(&mut self, id: u32) -> bool {
        if self.requests.front().is_some_and(|(next, _)| *next == id) {
            self.requests.clear();
            true
        } else {
            false
        }
    }
}

/// First file only: markL is a single-document editor. Resolve forwarded paths
/// against the *second process's* cwd, not the already-running app's cwd.
pub fn argument_path(args: impl IntoIterator<Item = OsString>, cwd: &Path) -> Option<PathBuf> {
    let mut literal = false;
    for arg in args.into_iter().skip(1) {
        if !literal && arg == "--" {
            literal = true;
            continue;
        }
        if arg.is_empty() || (!literal && arg.to_string_lossy().starts_with('-')) {
            continue;
        }
        if let Some(text) = arg.to_str().filter(|text| text.contains("://")) {
            return tauri::Url::parse(text).ok()?.to_file_path().ok();
        }
        let path = PathBuf::from(arg);
        return std::path::absolute(if path.is_absolute() {
            path
        } else {
            cwd.join(path)
        })
        .ok();
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<OsString> {
        values.iter().map(OsString::from).collect()
    }

    #[test]
    fn cli_resolves_relative_names_and_double_dash() {
        let cwd = std::env::temp_dir();
        assert_eq!(
            argument_path(args(&["markl", "notes with spaces.md", "ignored.md"]), &cwd),
            Some(cwd.join("notes with spaces.md"))
        );
        assert_eq!(
            argument_path(args(&["markl", "-psn_123", "--", "-draft.md"]), &cwd),
            Some(cwd.join("-draft.md"))
        );
        assert_eq!(argument_path(args(&["markl"]), &cwd), None);
        assert_eq!(
            argument_path(args(&["markl", "https://example.com/a.md"]), &cwd),
            None
        );
    }

    #[test]
    fn cli_accepts_local_file_urls() {
        let path = std::env::temp_dir().join("a #é.md");
        let url = tauri::Url::from_file_path(&path).unwrap();
        assert_eq!(
            argument_path(args(&["markl", url.as_str()]), Path::new(".")),
            Some(path)
        );
    }

    #[test]
    fn queue_preserves_order_and_checks_request_ids() {
        let mut pending = Pending::default();
        assert!(pending.push("one.md".into()));
        assert!(!pending.push("one.md".into()));
        assert!(pending.push("two.md".into()));
        let one = pending.next(None).unwrap();
        assert_eq!(pending.take(one + 10), None);
        assert!(!pending.cancel(one + 10));
        assert_eq!(pending.take(one), Some("one.md".into()));
        let two = pending.next(None).unwrap();
        assert_ne!(one, two);
        assert_eq!(pending.take(two), Some("two.md".into()));
        assert!(!pending.has_requests());
    }

    #[test]
    fn reopening_current_file_keeps_edits_and_cancel_clears_pending_burst() {
        let mut pending = Pending::default();
        pending.push("current.md".into());
        pending.push("next.md".into());
        pending.push("last.md".into());
        let id = pending.next(Some(Path::new("current.md"))).unwrap();
        assert!(pending.cancel(id));
        assert_eq!(pending.next(None), None);
    }

    #[test]
    fn queue_is_bounded_without_replacing_the_in_flight_head() {
        let mut pending = Pending::default();
        for index in 0..MAX_PENDING {
            assert!(pending.push(format!("{index}.md").into()));
        }
        let first = pending.next(None).unwrap();
        assert!(!pending.push("overflow.md".into()));
        assert_eq!(pending.take(first), Some("0.md".into()));
    }
}

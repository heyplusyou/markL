//! Read-only image protocol, scoped to the current document's directory.

use std::fs::File;
use std::io::Read;
use std::path::{Component, Path, PathBuf};

use percent_encoding::percent_decode_str;
use tauri::http::{Method, Request, Response, StatusCode};

const MAX_BYTES: u64 = 16 * 1024 * 1024;

fn resolve(document: &Path, source: &str) -> Option<PathBuf> {
    if source.len() > 4096 {
        return None;
    }
    // Split before decoding so photo%23one.png names a literal #.
    let source = source.split(['?', '#']).next()?;
    let source = percent_decode_str(source).decode_utf8().ok()?;
    if source.is_empty() || source.contains([':', '\\']) || source.chars().any(char::is_control) {
        return None;
    }
    let relative = Path::new(source.as_ref());
    if relative.components().any(|part| match part {
        Component::CurDir => false,
        Component::Normal(name) => name.to_string_lossy().starts_with('.'),
        _ => true,
    }) {
        return None;
    }
    let directory = document.parent()?.canonicalize().ok()?;
    let target = directory.join(relative).canonicalize().ok()?;
    (target.starts_with(&directory) && target.is_file()).then_some(target)
}

fn image_type(bytes: &[u8]) -> Option<&'static str> {
    if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some("image/png")
    } else if bytes.starts_with(b"\xff\xd8\xff") {
        Some("image/jpeg")
    } else if bytes.starts_with(b"GIF87a") || bytes.starts_with(b"GIF89a") {
        Some("image/gif")
    } else if bytes.starts_with(b"RIFF") && bytes.get(8..12) == Some(b"WEBP") {
        Some("image/webp")
    } else if bytes.get(4..8) == Some(b"ftyp")
        && matches!(bytes.get(8..12), Some(b"avif" | b"avis"))
    {
        Some("image/avif")
    } else {
        None
    }
}

fn read(document: &Path, source: &str) -> Option<(&'static str, Vec<u8>)> {
    let path = resolve(document, source)?;
    let file = File::open(path).ok()?;
    let metadata = file.metadata().ok()?;
    if !metadata.is_file() || metadata.len() > MAX_BYTES {
        return None;
    }
    let mut bytes = Vec::new();
    file.take(MAX_BYTES + 1).read_to_end(&mut bytes).ok()?;
    if bytes.len() as u64 > MAX_BYTES {
        return None;
    }
    Some((image_type(&bytes)?, bytes))
}

pub fn response(
    document: Option<&Path>,
    generation: u64,
    request: &Request<Vec<u8>>,
) -> Response<Vec<u8>> {
    let image = (|| {
        if request.method() != Method::GET || request.uri().path().len() > 16384 {
            return None;
        }
        // convertFileSrc encodes the entire "epoch/markdown URL" payload.
        let payload = percent_decode_str(request.uri().path().strip_prefix('/')?)
            .decode_utf8()
            .ok()?;
        let (epoch, source) = payload.split_once('/')?;
        if epoch.parse::<u64>().ok()? != generation {
            return None;
        }
        read(document?, source)
    })();
    let (status, content_type, bytes) = match image {
        Some((mime, bytes)) => (StatusCode::OK, mime, bytes),
        None => (
            StatusCode::NOT_FOUND,
            "text/plain; charset=utf-8",
            b"Image unavailable".to_vec(),
        ),
    };
    if std::env::var_os("MARKL_TIMING").is_some() {
        eprintln!(
            "markl-image status={} bytes={}",
            status.as_u16(),
            bytes.len()
        );
    }
    Response::builder()
        .status(status)
        .header("Content-Type", content_type)
        .header("X-Content-Type-Options", "nosniff")
        .header("Cache-Control", "no-store")
        .header("Content-Security-Policy", "default-src 'none'; sandbox")
        .body(bytes)
        .expect("static response headers")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn directory() -> PathBuf {
        static COUNTER: AtomicUsize = AtomicUsize::new(0);
        let path = std::env::temp_dir().join(format!(
            "markl-image-{}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn request(epoch: u64, source: &str) -> Request<Vec<u8>> {
        let payload = format!("{epoch}/{source}");
        let encoded =
            percent_encoding::utf8_percent_encode(&payload, percent_encoding::NON_ALPHANUMERIC);
        Request::builder()
            .uri(format!("markl-image://localhost/{encoded}"))
            .body(Vec::new())
            .unwrap()
    }

    #[test]
    fn serves_only_current_document_relative_images() {
        let root = directory();
        let document = root.join("notes.md");
        fs::create_dir_all(root.join("images")).unwrap();
        fs::write(root.join("images/a #é.png"), b"\x89PNG\r\n\x1a\npixel").unwrap();
        let result = response(
            Some(&document),
            7,
            &request(7, "images/a%20%23%C3%A9.png?x=1"),
        );
        assert_eq!(result.status(), StatusCode::OK);
        assert_eq!(result.headers()["content-type"], "image/png");
        assert_eq!(result.headers()["cache-control"], "no-store");
        assert_eq!(result.body(), b"\x89PNG\r\n\x1a\npixel");
        assert_eq!(
            response(Some(&document), 8, &request(7, "images/a%20%23%C3%A9.png")).status(),
            StatusCode::NOT_FOUND
        );
        assert_eq!(
            response(None, 7, &request(7, "images/a%20%23%C3%A9.png")).status(),
            StatusCode::NOT_FOUND
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rejects_traversal_absolute_remote_hidden_and_nonimage_sources() {
        let root = directory();
        let document = root.join("notes.md");
        fs::write(root.join("secret.png"), b"not an image").unwrap();
        fs::write(
            root.join("active.svg"),
            b"<svg><script>alert(1)</script></svg>",
        )
        .unwrap();
        for source in [
            "../outside.png",
            "%2e%2e/outside.png",
            "/etc/passwd",
            "C:/private.png",
            "\\\\server\\image.png",
            "http://example.com/a.png",
            "file:///a.png",
            "data:image/png;base64,a",
            ".hidden.png",
            "secret.png",
            "active.svg",
            "a%00.png",
        ] {
            assert_eq!(
                response(Some(&document), 1, &request(1, source)).status(),
                StatusCode::NOT_FOUND,
                "{source}"
            );
        }
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rejects_oversized_images() {
        let root = directory();
        let path = root.join("large.png");
        fs::write(&path, b"\x89PNG\r\n\x1a\n").unwrap();
        File::options()
            .write(true)
            .open(path)
            .unwrap()
            .set_len(MAX_BYTES + 1)
            .unwrap();
        assert!(read(&root.join("notes.md"), "large.png").is_none());
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symlinks_outside_the_document_directory() {
        let root = directory();
        let outside = directory();
        fs::write(outside.join("outside.png"), b"\x89PNG\r\n\x1a\npixel").unwrap();
        std::os::unix::fs::symlink(outside.join("outside.png"), root.join("link.png")).unwrap();
        assert!(read(&root.join("notes.md"), "link.png").is_none());
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(outside).unwrap();
    }
}

//! Reading and writing markdown files. markL opens UTF-8 text only and writes a file back with the line
//! endings and byte order mark it had.

use std::fs::{self, File};
use std::io::{self, Write};
use std::path::Path;

const BOM: &[u8] = b"\xEF\xBB\xBF";

/// How a file stores its text, so it can be written back the same way.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Format {
    pub bom: bool,
    /// Lines end in `\r\n`, judged by the first line break. Mixed files are saved with that one.
    pub crlf: bool,
}

/// Reads a file and returns its text (without the byte order mark) and format.
pub fn read(path: &Path) -> io::Result<(Vec<u8>, Format)> {
    decode(fs::read(path)?).map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            "markL can only open UTF-8 text files.",
        )
    })
}

/// Splits a file's bytes into text and format. The text keeps its line breaks; the editor normalizes
/// them to `\n`.
pub fn decode(mut bytes: Vec<u8>) -> Result<(Vec<u8>, Format), std::str::Utf8Error> {
    let bom = bytes.starts_with(BOM);
    if bom {
        bytes.drain(..BOM.len());
    }
    std::str::from_utf8(&bytes)?;
    let crlf = bytes
        .iter()
        .position(|&b| b == b'\n')
        .is_some_and(|i| i > 0 && bytes[i - 1] == b'\r');
    Ok((bytes, Format { bom, crlf }))
}

/// Turns editor text (UTF-8 with `\n` line breaks) into file bytes in the given format.
pub fn encode(text: &[u8], format: Format) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(BOM.len() + text.len() + text.len() / 16);
    if format.bom {
        bytes.extend_from_slice(BOM);
    }
    if format.crlf {
        for (i, line) in text.split(|&b| b == b'\n').enumerate() {
            if i > 0 {
                bytes.extend_from_slice(b"\r\n");
            }
            bytes.extend_from_slice(line);
        }
    } else {
        bytes.extend_from_slice(text);
    }
    bytes
}

/// Replaces the file at `path` with `bytes` so that it holds either the old or the new content, never
/// a mix: the bytes go to a temporary file next to it, which is then renamed over it. Writes through
/// symlinks to their target and keeps the file's permissions. Extended attributes (such as Finder
/// tags) are not carried over.
pub fn write_atomically(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let path = match fs::canonicalize(path) {
        Ok(target) => target,
        Err(err) if err.kind() == io::ErrorKind::NotFound => path.to_path_buf(),
        Err(err) => return Err(err),
    };
    let (Some(dir), Some(name)) = (path.parent(), path.file_name()) else {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "not a file path",
        ));
    };
    let temp = dir.join(format!(
        ".{}.markl-{}",
        name.to_string_lossy(),
        std::process::id()
    ));

    let written = (|| {
        let mut file = File::create(&temp)?;
        file.write_all(bytes)?;
        if let Ok(metadata) = fs::metadata(&path) {
            file.set_permissions(metadata.permissions())?;
        }
        file.sync_all()?;
        fs::rename(&temp, &path)
    })();
    if written.is_err() {
        let _ = fs::remove_file(&temp);
    }
    written?;

    // Make the rename itself durable.
    #[cfg(unix)]
    File::open(dir)?.sync_all()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// A fresh, empty directory for one test.
    fn temp_dir() -> PathBuf {
        static COUNTER: AtomicUsize = AtomicUsize::new(0);
        let dir = std::env::temp_dir().join(format!(
            "markl-test-{}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn decode_detects_format() {
        let (text, format) = decode(b"# Hi\nthere\r\n".to_vec()).unwrap();
        assert_eq!(text, b"# Hi\nthere\r\n");
        assert_eq!(format, Format::default());

        let (text, format) = decode(b"\xEF\xBB\xBFa\r\nb\n".to_vec()).unwrap();
        assert_eq!(text, b"a\r\nb\n");
        assert_eq!(
            format,
            Format {
                bom: true,
                crlf: true
            }
        );

        let (_, format) = decode(b"one line".to_vec()).unwrap();
        assert!(!format.crlf);
    }

    #[test]
    fn decode_rejects_non_utf8() {
        assert!(decode(b"caf\xE9".to_vec()).is_err());
    }

    #[test]
    fn encode_restores_format() {
        let text = "a\nb\n\nc".as_bytes();
        assert_eq!(encode(text, Format::default()), text);
        assert_eq!(
            encode(
                text,
                Format {
                    bom: true,
                    crlf: true
                }
            ),
            b"\xEF\xBB\xBFa\r\nb\r\n\r\nc"
        );
    }

    #[test]
    fn round_trip_is_exact() {
        for file in [
            &b"# Title\r\n\r\n- one\r\n- two\r\n"[..],
            b"\xEF\xBB\xBFplain\nunix\n",
            b"",
        ] {
            let (text, format) = decode(file.to_vec()).unwrap();
            let normalized = String::from_utf8(text).unwrap().replace("\r\n", "\n");
            assert_eq!(encode(normalized.as_bytes(), format), file);
        }
    }

    #[test]
    fn write_replaces_content_and_leaves_no_temp_file() {
        let dir = temp_dir();
        let path = dir.join("notes.md");
        fs::write(&path, "old").unwrap();
        write_atomically(&path, b"new").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"new");
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 1);
    }

    #[test]
    fn write_creates_new_files() {
        let path = temp_dir().join("new.md");
        write_atomically(&path, b"hello").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"hello");
    }

    #[cfg(unix)]
    #[test]
    fn write_keeps_permissions_and_follows_symlinks() {
        use std::os::unix::fs::{PermissionsExt, symlink};

        let dir = temp_dir();
        let target = dir.join("target.md");
        let link = dir.join("link.md");
        fs::write(&target, "old").unwrap();
        fs::set_permissions(&target, fs::Permissions::from_mode(0o600)).unwrap();
        symlink(&target, &link).unwrap();

        write_atomically(&link, b"new").unwrap();

        assert!(
            fs::symlink_metadata(&link)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert_eq!(fs::read(&target).unwrap(), b"new");
        let mode = fs::metadata(&target).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
    }
}

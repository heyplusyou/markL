## Early macOS release

A small, fast markdown editor. This version supports opening, editing and saving UTF-8 files, a recent-files
sidebar, markdown syntax highlighting and unsaved-changes prompts.

Inline rendering, drag and drop, "Open with" and external-change reloading are **not implemented yet**.
Windows and Linux installers are not included. Automated Windows checks do not certify native UI or performance.

### Install

1. Download the universal `.dmg` (Apple Silicon and Intel).
2. Open it and drag **markL** into **Applications**.
3. Launch markL. If macOS blocks it, open **System Settings → Privacy & Security → Open Anyway** and confirm.

**Signing:** ad-hoc signed, not Apple Developer ID signed or notarized. Do not disable Gatekeeper.

### Checksums, license and source

`SHA256SUMS.txt` contains the SHA-256 hashes of the downloads. To check a downloaded installer:

```sh
shasum -a 256 markL_*.dmg
```

Compare the result with the matching line in `SHA256SUMS.txt`.

markL is **GPL-3.0-only**, without warranty. The license and dependency notices are included in the app and
attached here. The `markL-v…-source.tar.gz` download contains the matching source, dependency lockfiles,
build scripts and build instructions in `README.md`. GitHub's "Source code" downloads provide the same tagged
project source. Dependency source locations are listed in `THIRD-PARTY-NOTICES.txt`.

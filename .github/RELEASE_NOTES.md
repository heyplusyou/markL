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

### Verification

The universal app's signature and both architectures were checked, the DMG was mounted and verified, and the
packaged app opened a 100 KB file successfully. Download size: about 4 MiB; installed app: about 9.1 MiB.
In a short warm-launch check on the current machine, median time to editable was 662 ms and memory after
2 seconds was 91 MiB, versus 646 ms and 88 MiB for the previous native build. Both exceeded the original
performance targets in this environment. Cold-boot and native Windows UI/performance checks remain outstanding.

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

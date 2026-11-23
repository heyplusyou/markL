## markL v0.2.0 — live Markdown and native file opening

A small Markdown editor for macOS. This early prerelease adds:

- Live inline headings, emphasis, links, lists, task lists, quotes and rules, with source revealed while editing.
- Undoable task checkboxes, lazy code highlighting, scoped local images and rendered Markdown tables.
- Drag and drop, macOS "Open with", and command-line files forwarded to the already-running window.
- Serialized document operations and unsaved-changes prompts that protect the buffer during external opens.

New/Open/Save/Save As, byte-preserving UTF-8 saves and the recent-files sidebar remain included.
External-change reloading is **not implemented yet**. A physical Finder drag gesture still needs manual
verification; automated native tests cover cold/warm "Open with" and single-instance CLI forwarding.
Windows and Linux installers are not included. Automated Windows checks do not certify native UI or performance.

### Install

1. Download the universal `.dmg` (Apple Silicon and Intel).
2. Open it and drag **markL** into **Applications**.
3. Launch markL. If macOS blocks it, open **System Settings → Privacy & Security → Open Anyway** and confirm.

**Signing:** ad-hoc signed, not Apple Developer ID signed or notarized. Do not disable Gatekeeper.

### Verification

The release workflow tests on macOS and Windows and checks both universal Mac architectures, the ad-hoc
signature and bundled licenses. The installer is verified locally before this draft is published.

The native Apple Silicon development build measured 605 ms to editable, 75.1 MiB total memory with a
100 KB file after 10 seconds idle, and 4.77 MiB installed (five warm runs). These are not universal-installer
measurements. Startup still exceeds the original target in this environment; memory and native app size
remain within budget. Chromium's 1 MB typing median was 5.4 ms. Cold-boot and native Windows UI/performance
checks remain outstanding. See the README for measurement methods and comparisons.

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

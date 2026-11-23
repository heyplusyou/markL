#!/usr/bin/env python3
"""Smoke-test cold/warm Open With and single-instance CLI forwarding on macOS.

No UI automation permission is needed. Refuses to run if any markL process exists,
closes only the instance it launches, and restores the user's recent-file list.
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import signal
import struct
import subprocess
import sys
import tempfile
import unicodedata
import zlib

ROOT = Path(__file__).resolve().parent.parent


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\0\0\xff")) + chunk(b"IEND", b"")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", nargs="?", type=Path, default=ROOT / "src-tauri/target/release/bundle/macos/markL.app")
    parser.add_argument("--temp-dir", type=Path, help="parent directory for temporary test fixtures")
    args = parser.parse_args()
    if sys.platform != "darwin":
        sys.exit("This test requires macOS")
    app = args.app.resolve()
    binary = app / "Contents/MacOS/markl"
    if not binary.is_file():
        sys.exit("Build the release app first: bun run tauri build --bundles app")
    processes = subprocess.check_output(["ps", "-A", "-o", "pid=,comm="], text=True)
    if any(Path(line.strip().partition(" ")[2].strip()).name == "markl" for line in processes.splitlines()):
        sys.exit("Close markL before running this test; existing user sessions will not be interrupted")

    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    associations = info.get("CFBundleDocumentTypes", [])
    extensions = {ext for item in associations for ext in item.get("CFBundleTypeExtensions", [])}
    assert {"md", "markdown", "mdown", "mkd", "txt"} <= extensions, associations
    assert all(item.get("CFBundleTypeRole") == "Editor" and item.get("LSHandlerRank") == "Alternate" for item in associations), associations

    spec = importlib.util.spec_from_file_location("measure", ROOT / "scripts/measure-macos.py")
    measure = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(measure)
    recent = Path.home() / "Library/Application Support/markL/recent.json"
    previous = recent.read_bytes() if recent.exists() else None
    with tempfile.TemporaryDirectory(prefix="markl-native-open-", dir=args.temp_dir) as directory:
        root = Path(directory)
        first = root / "first #é.md"
        first.write_text("# First\n\nCold Open With.\n")
        cwd = root / "different cwd"
        cwd.mkdir()
        second = cwd / "relative #é.md"
        second.write_text("# Second\n\nA relative CLI argument.\n")
        third = root / "third.md"
        third.write_text("# Third\n\nWarm Open With.\n\n![Local image](chart.png)\n")
        (root / "chart.png").write_bytes(png())
        log = root / "stderr.log"
        log.write_text("")

        def is_current(path):
            try:
                files = json.loads(recent.read_text())["files"]
                return bool(files) and unicodedata.normalize("NFC", files[0]) == unicodedata.normalize("NFC", str(path))
            except (OSError, ValueError):
                return False

        pid = None
        try:
            subprocess.run(["open", "-n", "--env", "MARKL_TIMING=1", "--stderr", str(log), "-a", str(app), str(first)], check=True)
            pid = measure.wait_for(lambda: measure.find_pid(binary), 15, "primary process")
            measure.wait_for(lambda: is_current(first) and "markl-timing " in log.read_text(), 15, "cold Open With")
            print("ok   cold Open With opens a Unicode/space-containing file")

            subprocess.run([str(binary), second.name], cwd=cwd, check=True, timeout=15, capture_output=True)
            measure.wait_for(lambda: is_current(second), 15, "forwarded CLI request")
            assert measure.find_pid(binary) == pid
            print("ok   relative CLI request reaches the existing process with its original cwd")

            subprocess.run(["open", "-a", str(app), str(third)], check=True)
            measure.wait_for(lambda: is_current(third) and "markl-image status=200" in log.read_text(), 15, "warm Open With and local image")
            assert measure.find_pid(binary) == pid
            assert log.read_text().count("markl-open loaded") == 3, log.read_text()
            print("ok   warm Open With reuses the process, loads the new file and refreshes image scope")
            print("ok   bundle declares alternate Markdown/text associations without replacing defaults")
        except BaseException:
            print("Native test stderr:\n" + log.read_text(), file=sys.stderr)
            raise
        finally:
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                measure.wait_for(lambda: measure.find_pid(binary) is None, 10, "test instance to quit")
            if previous is None:
                recent.unlink(missing_ok=True)
            else:
                recent.write_bytes(previous)
            print("Original recent-file list restored")


if __name__ == "__main__":
    main()

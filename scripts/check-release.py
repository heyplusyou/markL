#!/usr/bin/env python3
"""Refuse to release a tag that does not match all three app versions."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tag = sys.argv[1] if len(sys.argv) == 2 else ""
if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
    sys.exit("Expected a release tag such as v0.1.0")

versions = {
    "package.json": json.loads((ROOT / "package.json").read_text())["version"],
    "src-tauri/tauri.conf.json": json.loads((ROOT / "src-tauri/tauri.conf.json").read_text())["version"],
}
manifest = (ROOT / "src-tauri/Cargo.toml").read_text()
package_section = re.search(r"(?ms)^\[package\]\n(.*?)(?=^\[|\Z)", manifest)
version = re.search(r'^version\s*=\s*"([^"]+)"', package_section[1], re.MULTILINE) if package_section else None
if not version:
    sys.exit("Cannot find the package version in src-tauri/Cargo.toml")
versions["src-tauri/Cargo.toml"] = version[1]
for name, version in versions.items():
    if version != tag[1:]:
        sys.exit(f"{name} is {version}, but the tag is {tag}")
print(f"Release versions match {tag}")

#!/usr/bin/env python3
"""Collect Mac build/runtime license texts. Normal builds only check lockfile hashes.

Uses installed npm packages and Cargo's locked metadata. For crates that omit their
license files, fetch the upstream repository's license at the crate's recorded commit.
No build-time network access is needed once the reviewed notices are committed.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "THIRD-PARTY-NOTICES.txt"
PREFIXES = ("license", "licence", "copying", "copyright", "notice", "unlicense")


def fingerprints():
    return [
        f"{name} sha256: {hashlib.sha256((ROOT / name).read_bytes()).hexdigest()}"
        for name in ("bun.lock", "src-tauri/Cargo.lock")
    ]


def license_files(root):
    files = []
    for path in root.iterdir():
        if path.is_file() and path.name.lower().startswith(PREFIXES):
            files.append((path.name, path.read_text(encoding="utf-8")))
        elif path.is_dir() and path.name.lower() in ("licenses", "licences"):
            files.extend(
                (str(file.relative_to(root)), file.read_text(encoding="utf-8"))
                for file in path.rglob("*") if file.is_file()
            )
    return sorted(files)


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "markL-license-collector"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


@cache
def upstream_licenses(repository, revision):
    if not repository.startswith("https://github.com/") or not revision:
        raise RuntimeError(f"Need reviewed license files for {repository} at {revision}")
    slug = repository.removeprefix("https://github.com/").removesuffix(".git").rstrip("/")
    tree = json.loads(fetch(f"https://api.github.com/repos/{slug}/git/trees/{revision}"))
    files = []
    for entry in tree["tree"]:
        if entry["type"] == "blob" and entry["path"].lower().startswith(PREFIXES):
            url = f"https://raw.githubusercontent.com/{slug}/{revision}/{entry['path']}"
            files.append((url, fetch(url)))
    if not files:
        raise RuntimeError(f"No upstream license files for {repository} at {revision}")
    return sorted(files)


def npm_packages():
    packages = {}

    def visit(name, parent):
        directory = parent
        while True:
            manifest = directory / "node_modules" / name / "package.json"
            if manifest.exists():
                break
            if directory == ROOT or ROOT not in directory.parents:
                raise RuntimeError(f"Install dependency {name} first: bun install --frozen-lockfile")
            directory = directory.parent
        package = json.loads(manifest.read_text())
        key = (package["name"], package["version"])
        if key in packages:
            return
        files = license_files(manifest.parent)
        if not files:
            raise RuntimeError(f"No license file for npm package {key}")
        packages[key] = {
            "name": f"npm: {key[0]} {key[1]}",
            "license": package.get("license", "unspecified"),
            "source": f"https://registry.npmjs.org/{key[0]}/-/{key[0].split('/')[-1]}-{key[1]}.tgz",
            "files": files,
        }
        for dependency in package.get("dependencies", {}):
            visit(dependency, manifest.parent)

    for dependency in json.loads((ROOT / "package.json").read_text())["dependencies"]:
        visit(dependency, ROOT)
    return list(packages.values())


def rust_packages():
    packages = {}
    for target in ("aarch64-apple-darwin", "x86_64-apple-darwin"):
        metadata = json.loads(subprocess.check_output([
            "cargo", "metadata", "--manifest-path", str(ROOT / "src-tauri/Cargo.toml"),
            "--locked", "--format-version", "1", "--filter-platform", target,
        ]))
        resolved = {node["id"] for node in metadata["resolve"]["nodes"]}
        for package in metadata["packages"]:
            if package["source"] and package["id"] in resolved:
                packages[package["id"]] = package
    result = []
    for package in sorted(packages.values(), key=lambda p: (p["name"], p["version"])):
        root = Path(package["manifest_path"]).parent
        files = license_files(root)
        if not files:
            if (package["name"], package["version"], package["license"]) == ("selectors", "0.38.0", "MPL-2.0"):
                # This crate and its upstream repository omit a standalone MPL text.
                url = "https://www.mozilla.org/MPL/2.0/index.txt"
                files = [(url, fetch(url))]
            else:
                vcs = json.loads((root / ".cargo_vcs_info.json").read_text())
                files = upstream_licenses(package["repository"], vcs["git"]["sha1"])
        result.append({
            "name": f"Rust: {package['name']} {package['version']}",
            "license": package["license"] or "see license text",
            "source": f"https://crates.io/api/v1/crates/{package['name']}/{package['version']}/download",
            "files": files,
        })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="offline lockfile freshness check")
    args = parser.parse_args()
    if args.check:
        header = OUTPUT.read_text(encoding="utf-8").split("\n\n", 1)[0] if OUTPUT.exists() else ""
        if not all(line in header.splitlines() for line in fingerprints()):
            sys.exit("Dependency notices are missing or stale. Run bun run licenses and review the result.")
        print("Bundled dependency notices match both lockfiles")
        return

    packages = sorted(npm_packages() + rust_packages(), key=lambda p: p["name"])
    # Deduplicate identical license texts, preserving every package's attribution and source.
    texts = {}
    sections = ["markL third-party notices", *fingerprints(), "", (
        "Licenses for the universal macOS distribution, including build dependencies.\n"
        "Frontend runtime dependencies and the locked Mac Cargo dependency graph are listed below.\n"
        "Dependency source archives are available at each Source URL.\n"
        "Dependencies retain their own licenses; markL itself is GPL-3.0-only.\n"
        "Generated by scripts/licenses.py. Review before redistribution."
    )]
    for package in packages:
        sections.extend(["", package["name"], f"License: {package['license']}", f"Source: {package['source']}"])
        for filename, text in package["files"]:
            text = text.strip()
            digest = hashlib.sha256(text.encode()).hexdigest()
            if digest not in texts:
                texts[digest] = (f"L{len(texts) + 1}", text)
            sections.append(f"  {filename}: {texts[digest][0]}")
    for label, text in texts.values():
        sections.extend(["", f"===== {label} =====", text])
    OUTPUT.write_text("\n".join(sections) + "\n", encoding="utf-8")
    print(f"Wrote notices for {len(packages)} packages ({len(texts)} unique license texts)")


if __name__ == "__main__":
    main()

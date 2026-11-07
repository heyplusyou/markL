#!/usr/bin/env python3
"""Measure markL's startup time and memory on macOS.

Launches the release build through LaunchServices (like a double-click), reads the timing line markL prints with
MARKL_TIMING=1, waits until it is idle, then measures the memory footprint of the app together with the WebKit
processes macOS holds it responsible for (WebContent, Networking, GPU). Shared memory is counted once.

Usage: python3 scripts/measure-macos.py [--runs 5] [--idle 10] [--file notes.md] [path/to/markL.app]

With --file, markL opens that file on startup, and "ready" is when it can be edited.
"""

import argparse
import ctypes
import json
import os
import signal
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_APP = ROOT / "src-tauri/target/release/bundle/macos/markL.app"

_libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
_responsible_pid = _libc.responsibility_get_pid_responsible_for_pid
_responsible_pid.argtypes = [ctypes.c_int]
_responsible_pid.restype = ctypes.c_int


def wait_for(check, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.005)
    raise SystemExit(f"timed out waiting for {what}")


def find_pid(binary):
    """The process running exactly this executable (not one that merely mentions its path)."""
    target = os.path.realpath(binary)
    out = subprocess.run(["ps", "-A", "-o", "pid=,comm="], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if os.path.realpath(command.strip()) == target:
            return int(pid)
    return None


def read_timing(log):
    for line in log.read_text().splitlines():
        if line.startswith("markl-timing "):
            return dict(field.split("=") for field in line.split()[1:])
    return None


def app_pids(pid):
    """The app's own process and every process macOS holds it responsible for."""
    out = subprocess.run(["ps", "-A", "-o", "pid="], capture_output=True, text=True, check=True).stdout
    return [p for p in map(int, out.split()) if p == pid or _responsible_pid(p) == pid]


def footprint(pids):
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "footprint.json"
        args = ["footprint", "--noCategories", "-j", str(report)]
        for pid in pids:
            args += ["-p", str(pid)]
        subprocess.run(args, capture_output=True, check=True)
        data = json.loads(report.read_text())
    return data["total footprint"], [(p["name"], p["footprint"]) for p in data["processes"]]


def run_once(app, binary, idle, file):
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "stderr.log"
        log.touch()
        launched_ms = time.time() * 1000
        command = ["open", "-n", "--env", "MARKL_TIMING=1", "--stderr", str(log), str(app)]
        if file:
            command += ["--args", str(file.resolve())]
        subprocess.run(command, check=True)
        try:
            # Look for the process only after markL is ready: polling `ps` during startup would compete with it
            # for the CPU.
            timing = wait_for(lambda: read_timing(log), 10, "markL to report ready")
            time.sleep(idle)
            total, processes = footprint(app_pids(find_pid(binary)))
        finally:
            pid = find_pid(binary)
            if pid:
                os.kill(pid, signal.SIGTERM)
            wait_for(lambda: find_pid(binary) is None, 10, "markL to quit")
    return {
        "main_ms": int(timing["main"]) - launched_ms,
        "shown_ms": int(timing["shown"]) - launched_ms,
        "ready_ms": int(timing["ready"]) - launched_ms,
        "total": total,
        "processes": processes,
    }


def mb(n):
    return f"{n / 1024 / 1024:.1f} MB"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app", nargs="?", type=Path, default=DEFAULT_APP)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--idle", type=float, default=10, help="seconds to wait before measuring memory")
    parser.add_argument("--file", type=Path, help="file to open on startup")
    args = parser.parse_args()

    binary = args.app / "Contents/MacOS/markl"
    if not binary.exists():
        raise SystemExit(f"{binary} not found; build first with `bun run tauri build --bundles app`")
    if find_pid(binary):
        raise SystemExit("markL is already running; quit it first")

    runs = []
    for i in range(args.runs):
        run = run_once(args.app, binary, args.idle, args.file)
        runs.append(run)
        print(f"run {i + 1}: main {run['main_ms']:.0f} ms, window shown {run['shown_ms']:.0f} ms, "
              f"ready {run['ready_ms']:.0f} ms, memory {mb(run['total'])}")
        time.sleep(1)

    last = runs[-1]
    print("\nprocesses (last run):")
    for name, size in sorted(last["processes"], key=lambda p: -p[1]):
        print(f"  {name:32} {mb(size):>9}")
    median = lambda key: statistics.median(r[key] for r in runs)
    print(f"\nmedian: window shown {median('shown_ms'):.0f} ms, ready {median('ready_ms'):.0f} ms, "
          f"memory {mb(median('total'))}")

    bundle = sum(f.stat().st_size for f in args.app.rglob("*") if f.is_file())
    print(f"size: app bundle {mb(bundle)}, binary {mb(binary.stat().st_size)}")


if __name__ == "__main__":
    main()

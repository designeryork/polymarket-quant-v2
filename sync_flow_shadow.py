#!/usr/bin/env python3
"""Sync the read-only ``flow-shadow`` data branch into the local research archive.

The collector publishes one small JSONL file per 30-minute run. This script
clones the data branch at depth 1 and copies new observation files into
``_research_archive/flow_shadow/observations``. It never touches the paper
engine, ledgers, gh-pages, or state-snapshot.

Usage:
    python sync_flow_shadow.py
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
DEFAULT_REPO = "designeryork/polymarket-quant-v2"
DEFAULT_DEST = Path(r"D:\2026_APPs by AI\_research_archive\flow_shadow")


def run(cmd: list[str], cwd: str | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync flow-shadow branch to local archive")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--dest", default=str(DEFAULT_DEST))
    parser.add_argument("--branch", default="flow-shadow")
    args = parser.parse_args()

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{args.repo}.git"
    with tempfile.TemporaryDirectory(prefix="flow-shadow-sync-") as tmp:
        work = Path(tmp) / "repo"
        clone = run(
            [
                "git",
                "-c",
                "http.proxy=",
                "-c",
                "https.proxy=",
                "clone",
                "--depth",
                "1",
                "--branch",
                args.branch,
                url,
                str(work),
            ]
        )
        if clone.returncode != 0:
            print(
                f"[sync_flow_shadow] branch unavailable: {(clone.stderr or '').strip()[:300]}",
                flush=True,
            )
            return 0
        source = work / "flow_shadow" / "observations"
        if not source.exists():
            print("[sync_flow_shadow] no observations directory", flush=True)
            return 0
        copied = 0
        skipped = 0
        for path in sorted(source.rglob("*.jsonl")):
            relative = path.relative_to(source)
            target = dest / "observations" / relative
            if target.exists() and target.stat().st_size == path.stat().st_size:
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
        latest = work / "flow_shadow" / "latest.json"
        if latest.exists():
            shutil.copy2(latest, dest / "latest.json")
        print(
            f"[sync_flow_shadow] copied={copied} skipped={skipped} dest={dest}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

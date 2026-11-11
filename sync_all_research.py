#!/usr/bin/env python3
"""Sync both v1 and v2 research logs into separate local archives.

v1: designeryork/polymarket-quant     -> _research_archive\\v1
v2: designeryork/polymarket-quant-v2  -> _research_archive\\v2

Run manually:
    python sync_all_research.py
The Windows scheduled task runs this via pythonw.exe.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
JOBS = [
    ("designeryork/polymarket-quant", r"D:\2026_APPs by AI\_research_archive\v1"),
    ("designeryork/polymarket-quant-v2", r"D:\2026_APPs by AI\_research_archive\v2"),
]


def main() -> int:
    rc = 0
    for repo, dest in JOBS:
        print(f"== sync {repo} -> {dest}", flush=True)
        result = subprocess.run(
            [
                sys.executable,
                str(BASE / "sync_research.py"),
                "--repo",
                repo,
                "--dest",
                dest,
                "--once",
            ]
        )
        rc = rc or result.returncode

    # Flow-shadow is a separate forward-only data branch; sync it too.
    print("== sync flow shadow", flush=True)
    flow_sync = subprocess.run(
        [sys.executable, str(BASE / "sync_flow_shadow.py")]
    )
    if flow_sync.returncode != 0:
        print("[sync_all_research] flow-shadow sync failed (ignored)", flush=True)

    # Phase 2.1: resolve every archived EVAL token to 1/0 after syncing.
    # --limit 0 covers all unresolved tokens, so nothing is permanently skipped.
    print("== resolve research tokens", flush=True)
    resolve = subprocess.run(
        [
            sys.executable,
            str(BASE / "resolve_research.py"),
            "--archive",
            r"D:\2026_APPs by AI\_research_archive",
            "--variant",
            "all",
            "--limit",
            "200",
            "--sleep",
            "0.1",
        ]
    )
    rc = rc or resolve.returncode

    # Phase 2.2: daily calibration report after resolution updates.
    print("== daily metrics", flush=True)
    metrics = subprocess.run(
        [
            sys.executable,
            str(BASE / "daily_metrics.py"),
            "--archive",
            r"D:\2026_APPs by AI\_research_archive",
            "--variant",
            "all",
            "--bootstrap",
            "2000",
        ]
    )
    rc = rc or metrics.returncode

    # Phase 2.3: A/B observation report (v1 vs v2).
    print("== A/B report", flush=True)
    ab = subprocess.run(
        [
            sys.executable,
            str(BASE / "ab_report.py"),
            "--archive",
            r"D:\2026_APPs by AI\_research_archive",
            "--period",
            datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        ]
    )
    rc = rc or ab.returncode

    # Flow-shadow pre-registered report (only meaningful after 300 resolved).
    print("== flow shadow report", flush=True)
    flow_report = subprocess.run(
        [
            sys.executable,
            str(BASE / "flow_shadow_report.py"),
            "--bootstrap",
            "2000",
        ]
    )
    if flow_report.returncode != 0:
        print("[sync_all_research] flow-shadow report failed (ignored)", flush=True)

    # Publish research progress to gh-pages for the dashboard panel.
    print("== research status", flush=True)
    status_result = subprocess.run(
        [sys.executable, str(BASE / "publish_research_status.py")]
    )
    if status_result.returncode != 0:
        print("[sync_all_research] research status publish failed (ignored)", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

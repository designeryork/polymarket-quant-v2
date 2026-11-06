#!/usr/bin/env python3
"""Publish dashboard_paper.html to this repository's gh-pages branch.

Used by cloud_runner.py every sync interval. This removes the dependency on a
GitHub Actions ``schedule`` cron for the 5-minute dashboard refresh.

Environment:
    GITHUB_TOKEN / GH_TOKEN   token with contents: write
    GITHUB_REPOSITORY         owner/repo (set automatically in Actions)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def run(cmd: list[str], cwd: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--dashboard", default="dashboard_paper.html")
    parser.add_argument("--branch", default="gh-pages")
    args = parser.parse_args()

    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    repo = args.repo or os.getenv("GITHUB_REPOSITORY", "")
    if not token or not repo:
        print("[publish_pages_self] no GITHUB_TOKEN/GITHUB_REPOSITORY; skip", flush=True)
        return 0

    dashboard = Path(args.dashboard)
    if not dashboard.exists():
        print(f"[publish_pages_self] dashboard missing: {dashboard}", flush=True)
        return 0

    work = Path(tempfile.mkdtemp(prefix="pages-self-"))
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    try:
        clone = run(["git", "clone", "--depth", "1", "--branch", args.branch, url, str(work)])
        if clone.returncode != 0:
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            run(["git", "init", "-b", args.branch], cwd=str(work))
            run(["git", "remote", "add", "origin", url], cwd=str(work))

        shutil.copy2(dashboard, work / "index.html")
        (work / ".nojekyll").write_text("", encoding="utf-8")
        run(["git", "config", "user.name", "polymarket-quant-bot"], cwd=str(work))
        run(["git", "config", "user.email", "bot@users.noreply.github.com"], cwd=str(work))
        run(["git", "add", "index.html", ".nojekyll"], cwd=str(work))
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        commit = run(
            ["git", "commit", "-m", f"update paper v2 dashboard {stamp}"],
            cwd=str(work),
        )
        if commit.returncode != 0:
            print("[publish_pages_self] no changes", flush=True)
            return 0
        push = run(["git", "push", "origin", f"HEAD:refs/heads/{args.branch}"], cwd=str(work))
        if push.returncode != 0:
            print(
                f"[publish_pages_self] push failed: {(push.stderr or '').strip()[:300]}",
                flush=True,
            )
            return 1
        print("[publish_pages_self] published", flush=True)
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

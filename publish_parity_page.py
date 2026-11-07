#!/usr/bin/env python3
"""Publish the generated parity page to the repository's gh-pages branch.

Unlike the dashboard publisher this script does **not** force-push: it clones
the existing gh-pages branch, updates only ``parity.html`` / ``parity.json``
(and an optional dated archive), and rebases/retries if the dashboard
publisher pushed in the meantime. This keeps ``index.html`` intact.

Environment:
    GITHUB_TOKEN / GH_TOKEN   token with contents: write
    GITHUB_REPOSITORY         owner/repo
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def run(cmd: list[str], cwd: str | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
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
    parser = argparse.ArgumentParser(description="Publish parity page to gh-pages")
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--html", default="state/parity/parity.html")
    parser.add_argument("--json", default="")
    parser.add_argument("--branch", default="gh-pages")
    parser.add_argument("--no-archive", action="store_true")
    args = parser.parse_args()

    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    repo = args.repo or os.getenv("GITHUB_REPOSITORY", "")
    if not token or not repo:
        print("[publish_parity_page] no GITHUB_TOKEN/GITHUB_REPOSITORY; skip", flush=True)
        return 0

    html_path = Path(args.html)
    if not html_path.exists():
        print(f"[publish_parity_page] html missing: {html_path}", flush=True)
        return 1
    json_path = Path(args.json) if args.json else None
    if json_path is not None and not json_path.exists():
        print(f"[publish_parity_page] json missing: {json_path}", flush=True)
        return 1

    work = Path(tempfile.mkdtemp(prefix="pages-parity-"))
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    try:
        clone = run(["git", "clone", "--depth", "1", "--branch", args.branch, url, str(work)])
        if clone.returncode != 0:
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            run(["git", "init", "-b", args.branch], cwd=str(work))
            run(["git", "remote", "add", "origin", url], cwd=str(work))

        shutil.copy2(html_path, work / "parity.html")
        (work / ".nojekyll").write_text("", encoding="utf-8")
        staged = ["parity.html", ".nojekyll"]
        if json_path is not None:
            shutil.copy2(json_path, work / "parity.json")
            staged.append("parity.json")
            if not args.no_archive:
                archive = work / "parity" / "archive"
                archive.mkdir(parents=True, exist_ok=True)
                shutil.copy2(json_path, archive / json_path.name)
                staged.append(f"parity/archive/{json_path.name}")

        run(["git", "config", "user.name", "polymarket-quant-bot"], cwd=str(work))
        run(["git", "config", "user.email", "bot@users.noreply.github.com"], cwd=str(work))
        run(["git", "add", *staged], cwd=str(work))
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        commit = run(
            ["git", "commit", "-m", f"update parity scan page {stamp}"],
            cwd=str(work),
        )
        if commit.returncode != 0:
            print("[publish_parity_page] no changes", flush=True)
            return 0

        # The dashboard publisher may push index.html between our clone and push.
        # Rebase and retry instead of force-pushing (which would delete it).
        last_error = ""
        for attempt in range(1, 4):
            pull = run(
                ["git", "pull", "--rebase", "--autostash", "origin", args.branch],
                cwd=str(work),
            )
            if pull.returncode != 0 and "couldn't find remote ref" not in (pull.stderr or "").lower():
                last_error = (pull.stderr or pull.stdout or "").strip()[:300]
                print(f"[publish_parity_page] pull attempt {attempt} failed: {last_error}", flush=True)
                continue
            push = run(
                ["git", "push", "origin", f"HEAD:refs/heads/{args.branch}"],
                cwd=str(work),
            )
            if push.returncode == 0:
                print("[publish_parity_page] published", flush=True)
                return 0
            last_error = (push.stderr or push.stdout or "").strip()[:300]
            print(f"[publish_parity_page] push attempt {attempt} failed: {last_error}", flush=True)
        print(f"[publish_parity_page] failed: {last_error}", flush=True)
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

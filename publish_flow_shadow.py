#!/usr/bin/env python3
"""Publish a flow-shadow JSONL snapshot to the dedicated ``flow-shadow`` branch.

This branch is data-only and is never force-pushed: each run adds one small
JSONL file under ``flow_shadow/observations/<date>/``. The paper dashboard
branch (gh-pages) and state snapshot are untouched.

Environment:
    GITHUB_TOKEN / GH_TOKEN   token with contents: write
    GITHUB_REPOSITORY         owner/repo
"""
from __future__ import annotations

import argparse
import json
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


def newest(data_dir: Path, pattern: str) -> Path | None:
    rows = sorted(data_dir.glob(pattern), key=lambda path: path.stat().st_mtime)
    return rows[-1] if rows else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish flow-shadow data to its own branch")
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--data-dir", default="state/flow_shadow")
    parser.add_argument("--branch", default="flow-shadow")
    parser.add_argument("--jsonl", default="")
    parser.add_argument("--meta", default="")
    args = parser.parse_args()

    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    repo = args.repo or os.getenv("GITHUB_REPOSITORY", "")
    if not token or not repo:
        print("[publish_flow_shadow] no GITHUB_TOKEN/GITHUB_REPOSITORY; skip", flush=True)
        return 0

    data_dir = Path(args.data_dir)
    jsonl = Path(args.jsonl) if args.jsonl else newest(data_dir, "flow_shadow_*.jsonl")
    meta = Path(args.meta) if args.meta else newest(data_dir, "flow_shadow_*.meta.json")
    if jsonl is None or not jsonl.exists():
        print(f"[publish_flow_shadow] jsonl missing in {data_dir}", flush=True)
        return 1
    if meta is not None and not meta.exists():
        meta = None

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    work = Path(tempfile.mkdtemp(prefix="flow-shadow-"))
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    try:
        clone = run(["git", "clone", "--depth", "1", "--branch", args.branch, url, str(work)])
        if clone.returncode != 0:
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            run(["git", "init", "-b", args.branch], cwd=str(work))
            run(["git", "remote", "add", "origin", url], cwd=str(work))

        target = work / "flow_shadow" / "observations" / day / jsonl.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(jsonl, target)
        staged = [str(target.relative_to(work)).replace("\\", "/")]

        if meta is not None:
            latest = work / "flow_shadow" / "latest.json"
            latest.parent.mkdir(parents=True, exist_ok=True)
            payload = json.loads(meta.read_text(encoding="utf-8"))
            payload["jsonl_branch_path"] = str(target.relative_to(work)).replace("\\", "/")
            latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            staged.append("flow_shadow/latest.json")

        run(["git", "config", "user.name", "polymarket-quant-bot"], cwd=str(work))
        run(["git", "config", "user.email", "bot@users.noreply.github.com"], cwd=str(work))
        run(["git", "add", *staged], cwd=str(work))
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        commit = run(
            ["git", "commit", "-m", f"flow-shadow snapshot {stamp}"],
            cwd=str(work),
        )
        if commit.returncode != 0:
            print("[publish_flow_shadow] no changes", flush=True)
            return 0

        for attempt in range(1, 4):
            pull = run(
                ["git", "pull", "--rebase", "--autostash", "origin", args.branch],
                cwd=str(work),
            )
            if pull.returncode != 0 and "couldn't find remote ref" not in (pull.stderr or "").lower():
                print(
                    f"[publish_flow_shadow] pull attempt {attempt} failed: "
                    f"{(pull.stderr or '').strip()[:200]}",
                    flush=True,
                )
                continue
            push = run(
                ["git", "push", "origin", f"HEAD:refs/heads/{args.branch}"],
                cwd=str(work),
            )
            if push.returncode == 0:
                print(f"[publish_flow_shadow] published {target.name}", flush=True)
                return 0
            print(
                f"[publish_flow_shadow] push attempt {attempt} failed: "
                f"{(push.stderr or '').strip()[:200]}",
                flush=True,
            )
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Archive paper-v2 research artifacts into monthly GitHub Releases.

Why
---
Actions artifacts expire (30 days in this repo). Releases are permanent:
one asset per run, stored under ``research-archive-YYYY-MM`` releases.
This keeps long-term history without growing the git repository.

What it does
------------
1. Lists unexpired artifacts named ``research-logs-*``.
2. Skips runs already present as ``run-<run_id>.tar.gz`` in any
   ``research-archive-*`` release.
3. Downloads the missing artifact, bundles it as
   ``run-<run_id>.tar.gz`` with a ``manifest.json``, and uploads it to the
   release for the artifact's UTC month.

Usage
-----
    python archive_research.py --repo owner/repo
    python archive_research.py --repo owner/repo --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ARTIFACT_PREFIX = "research-logs-"
RELEASE_PREFIX = "research-archive-"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    print(f"[{now_utc()}] {message}", flush=True)


def gh(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def gh_ok(args: list[str], timeout: int = 900) -> str:
    result = gh(args, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip()[:600])
    return result.stdout


def list_artifacts(repo: str) -> list[dict]:
    jq = (
        "{id, name, size_in_bytes, created_at, expired, "
        "run_id: .workflow_run.id}"
    )
    output = gh_ok(
        [
            "api",
            "--paginate",
            f"repos/{repo}/actions/artifacts?per_page=100",
            "--jq",
            f".artifacts[] | {jq}",
        ]
    )
    rows = [json.loads(line) for line in output.splitlines() if line.strip()]
    rows = [
        row
        for row in rows
        if not row.get("expired") and str(row.get("name", "")).startswith(ARTIFACT_PREFIX)
    ]
    rows.sort(key=lambda row: row.get("created_at") or "")
    return rows


def list_archived(repo: str) -> dict[str, dict]:
    """Return {asset_name: {tag, release_created_at}} for research-archive releases."""
    jq = "{tag_name, assets: [.assets[] | {name}]}"
    output = gh_ok(
        [
            "api",
            "--paginate",
            f"repos/{repo}/releases?per_page=100",
            "--jq",
            f".[] | {jq}",
        ]
    )
    archived: dict[str, dict] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        tag = str(row.get("tag_name") or "")
        if not tag.startswith(RELEASE_PREFIX):
            continue
        for asset in row.get("assets") or []:
            name = str(asset.get("name") or "")
            if name:
                archived[name] = {"tag": tag}
    return archived


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def count_lines(path: Path) -> int | None:
    if path.suffix not in (".jsonl", ".log", ".txt"):
        return None
    try:
        with open(path, "rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return None


def ensure_release(repo: str, tag: str, dry_run: bool) -> None:
    probe = gh(["release", "view", tag, "-R", repo])
    if probe.returncode == 0:
        return
    log(f"  create release {tag}")
    if dry_run:
        return
    gh_ok(
        [
            "release",
            "create",
            tag,
            "-R",
            repo,
            "--title",
            tag,
            "--notes",
            "Automated archive of paper-v2 research logs (one asset per run).",
        ]
    )


def build_tarball(artifact_dir: Path, run_id: str, artifact: dict, out_path: Path) -> dict:
    files = []
    work = Path(tempfile.mkdtemp(prefix=f"research-{run_id}-"))
    try:
        root = work / f"run-{run_id}"
        root.mkdir(parents=True, exist_ok=True)
        for src in sorted(artifact_dir.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(artifact_dir)
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            files.append(
                {
                    "path": str(rel).replace(os.sep, "/"),
                    "bytes": dst.stat().st_size,
                    "lines": count_lines(dst),
                    "sha256": sha256_file(dst),
                }
            )
        manifest = {
            "run_id": run_id,
            "artifact_name": artifact.get("name"),
            "artifact_created_at": artifact.get("created_at"),
            "artifact_size_in_bytes": artifact.get("size_in_bytes"),
            "archived_at": now_utc(),
            "files": files,
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with tarfile.open(out_path, "w:gz") as tar:
            tar.add(root, arcname=f"run-{run_id}")
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive paper-v2 research artifacts to Releases")
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", "designeryork/polymarket-quant-v2"))
    parser.add_argument("--limit", type=int, default=50, help="max artifacts per run (0 = all)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if gh(["auth", "status"]).returncode != 0:
        log("ERROR: gh 未登录；请先 gh auth login 或设置 GH_TOKEN")
        return 2

    artifacts = list_artifacts(args.repo)
    archived = list_archived(args.repo)
    pending = [a for a in artifacts if f"run-{a.get('run_id')}.tar.gz" not in archived]
    if args.limit > 0:
        pending = pending[-args.limit:]
    log(f"repo={args.repo} artifacts={len(artifacts)} archived={len(archived)} pending={len(pending)} dry_run={args.dry_run}")

    failures = 0
    for artifact in pending:
        run_id = str(artifact.get("run_id") or "")
        name = str(artifact.get("name") or "")
        created = str(artifact.get("created_at") or "")
        if not run_id or not created:
            log(f"  skip invalid artifact: {artifact}")
            continue
        month = created[:7]
        tag = f"{RELEASE_PREFIX}{month}"
        asset_name = f"run-{run_id}.tar.gz"
        log(f"  archive {name} -> {tag}/{asset_name}")

        tmp = Path(tempfile.mkdtemp(prefix=f"dl-{run_id}-"))
        try:
            if not args.dry_run:
                download = gh(
                    ["run", "download", run_id, "-R", args.repo, "-n", name, "-D", str(tmp)],
                    timeout=900,
                )
                if download.returncode != 0:
                    failures += 1
                    log(f"  download FAILED: {(download.stderr or '').strip()[:300]}")
                    continue
            ensure_release(args.repo, tag, args.dry_run)
            if args.dry_run:
                continue
            out_path = tmp / asset_name
            manifest = build_tarball(tmp, run_id, artifact, out_path)
            upload = gh(
                ["release", "upload", tag, str(out_path), "-R", args.repo, "--clobber"],
                timeout=900,
            )
            if upload.returncode != 0:
                failures += 1
                log(f"  upload FAILED: {(upload.stderr or '').strip()[:300]}")
                continue
            log(
                f"  uploaded {tag}/{asset_name} "
                f"({out_path.stat().st_size} bytes, files={len(manifest['files'])})"
            )
        except Exception as exc:
            failures += 1
            log(f"  FAILED: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    log(f"done pending={len(pending)} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

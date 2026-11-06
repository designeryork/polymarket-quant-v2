#!/usr/bin/env python3
"""Sync paper-v2 research logs from GitHub to a local archive.

What it does
------------
1. Lists non-expired GitHub Actions artifacts named ``research-logs-*`` and
   downloads the ones not seen before.
2. Lists GitHub Releases whose tag starts with ``research-`` and downloads
   their ``.tar.gz`` assets.
3. Writes ``sync_state.json`` (idempotency) and ``index.json`` (manifest).

Usage
-----
    python sync_research.py --once          # run once (default)
    python sync_research.py --dry-run       # show what would be downloaded
    python sync_research.py --artifacts-only
    python sync_research.py --releases-only

The script is intentionally one-shot: use Windows Task Scheduler to run it
daily (and at logon). It does not need admin rights.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REPO = "designeryork/polymarket-quant-v2"
DEFAULT_DEST = Path(r"D:\2026_APPs by AI\_research_archive\v2")
ARTIFACT_PREFIX = "research-logs-"
RELEASE_PREFIX = "research-"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_gh() -> str | None:
    found = shutil.which("gh")
    if found:
        return found
    candidates = [
        r"C:\Program Files\GitHub CLI\gh.exe",
        r"C:\Users\York\AppData\Local\Programs\GitHub CLI\gh.exe",
        r"C:\ProgramData\chocolatey\bin\gh.exe",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


GH = find_gh()
DEST = DEFAULT_DEST
LOG_FILE = DEST / "sync.log"


def log(message: str) -> None:
    line = f"[{now_utc()}] {message}"
    print(line, flush=True)
    try:
        DEST.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def gh(args: list[str], timeout: int = 300) -> str:
    if not GH:
        raise RuntimeError("找不到 gh CLI；请先安装 GitHub CLI 并登录")
    result = run([GH, *args], timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip()[:500])
    return result.stdout


def load_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def file_manifest(root: Path) -> list[dict]:
    files: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace(os.sep, "/")
        files.append(
            {
                "path": rel,
                "bytes": path.stat().st_size,
                "lines": count_lines(path) if path.suffix in (".jsonl", ".log", ".txt") else None,
                "sha256": sha256_file(path),
            }
        )
    return files


def list_artifacts(repo: str) -> list[dict]:
    jq = (
        "{id, name, size_in_bytes, created_at, expired, digest, "
        "run_id: .workflow_run.id}"
    )
    output = gh(
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


def download_artifacts(repo: str, state: dict, limit: int, dry_run: bool) -> int:
    rows = list_artifacts(repo)
    pending = [row for row in rows if str(row.get("id")) not in state.get("artifacts", {})]
    if limit > 0:
        pending = pending[-limit:]
    log(f"artifacts: total={len(rows)} pending={len(pending)}")
    downloaded = 0
    for row in pending:
        artifact_id = str(row.get("id"))
        run_id = str(row.get("run_id") or "")
        name = str(row.get("name") or "")
        target = DEST / "artifacts" / run_id / name
        log(f"  download {name} (run {run_id}, {row.get('size_in_bytes')} bytes) -> {target}")
        if dry_run:
            continue
        try:
            if target.exists() and not any(target.rglob("*.jsonl")):
                shutil.rmtree(target, ignore_errors=True)
            target.mkdir(parents=True, exist_ok=True)
            result = run(
                [GH, "run", "download", run_id, "-R", repo, "-n", name, "-D", str(target)],
                timeout=600,
            )
            if result.returncode != 0:
                log(f"  FAILED: {(result.stderr or '').strip()[:300]}")
                continue
            files = file_manifest(target)
            if not files:
                log("  WARNING: artifact extracted but no files found")
            state.setdefault("artifacts", {})[artifact_id] = {
                "id": artifact_id,
                "name": name,
                "run_id": run_id,
                "created_at": row.get("created_at"),
                "size_in_bytes": row.get("size_in_bytes"),
                "digest": row.get("digest"),
                "downloaded_at": now_utc(),
                "files": files,
            }
            downloaded += 1
        except Exception as exc:
            log(f"  FAILED: {type(exc).__name__}: {exc}")
    return downloaded


def list_releases(repo: str) -> list[dict]:
    jq = (
        "{tag_name, created_at, assets: [.assets[] | "
        "{id, name, size, created_at}]}"
    )
    output = gh(
        [
            "api",
            "--paginate",
            f"repos/{repo}/releases?per_page=100",
            "--jq",
            f".[] | {jq}",
        ]
    )
    rows = [json.loads(line) for line in output.splitlines() if line.strip()]
    rows = [row for row in rows if str(row.get("tag_name", "")).startswith(RELEASE_PREFIX)]
    return rows


def download_releases(repo: str, state: dict, dry_run: bool) -> int:
    rows = list_releases(repo)
    log(f"releases: total={len(rows)}")
    downloaded = 0
    for release in rows:
        tag = str(release.get("tag_name"))
        target = DEST / "releases" / tag
        for asset in release.get("assets") or []:
            name = str(asset.get("name") or "")
            if not name.endswith((".tar.gz", ".tgz", ".zip", ".jsonl.gz")):
                continue
            key = f"{tag}/{name}"
            if key in state.get("releases", {}) and (target / name).exists():
                continue
            log(f"  download {key} ({asset.get('size')} bytes) -> {target / name}")
            if dry_run:
                continue
            try:
                target.mkdir(parents=True, exist_ok=True)
                result = run(
                    [GH, "release", "download", tag, "-R", repo, "-p", name,
                     "-D", str(target), "--clobber"],
                    timeout=600,
                )
                if result.returncode != 0:
                    log(f"  FAILED: {(result.stderr or '').strip()[:300]}")
                    continue
                path = target / name
                state.setdefault("releases", {})[key] = {
                    "tag": tag,
                    "name": name,
                    "size": asset.get("size"),
                    "downloaded_at": now_utc(),
                    "bytes": path.stat().st_size if path.exists() else None,
                    "sha256": sha256_file(path) if path.exists() else None,
                }
                downloaded += 1
            except Exception as exc:
                log(f"  FAILED: {type(exc).__name__}: {exc}")
    return downloaded


def write_index(repo: str, state: dict) -> None:
    artifacts = list(state.get("artifacts", {}).values())
    releases = list(state.get("releases", {}).values())
    total_bytes = 0
    for item in artifacts:
        total_bytes += int(item.get("size_in_bytes") or 0)
    for item in releases:
        total_bytes += int(item.get("bytes") or 0)
    index = {
        "updated_at": now_utc(),
        "repo": repo,
        "dest": str(DEST),
        "artifact_count": len(artifacts),
        "release_asset_count": len(releases),
        "total_bytes": total_bytes,
        "artifacts": artifacts,
        "releases": releases,
    }
    save_json(DEST / "index.json", index)
    log(f"index written: {DEST / 'index.json'} ({len(artifacts)} artifacts, {len(releases)} release assets)")


def main() -> int:
    global DEST

    parser = argparse.ArgumentParser(description="Sync paper-v2 research logs from GitHub")
    parser.add_argument("--once", action="store_true", help="run once (default)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be downloaded")
    parser.add_argument("--artifacts-only", action="store_true")
    parser.add_argument("--releases-only", action="store_true")
    parser.add_argument("--limit", type=int, default=50, help="max new artifacts per run (0 = all)")
    parser.add_argument("--repo", default=os.getenv("RESEARCH_REPO", DEFAULT_REPO))
    parser.add_argument("--dest", default=os.getenv("RESEARCH_ARCHIVE_DIR", str(DEFAULT_DEST)))
    args = parser.parse_args()

    DEST = Path(args.dest)
    DEST.mkdir(parents=True, exist_ok=True)

    if not GH:
        log("ERROR: 找不到 gh CLI；请安装 GitHub CLI 并 gh auth login")
        return 2
    try:
        auth = run([GH, "auth", "status"])
        if auth.returncode != 0:
            log("ERROR: gh 未登录；请运行 gh auth login")
            return 2
    except Exception as exc:
        log(f"ERROR: gh auth 检查失败: {exc}")
        return 2

    state_path = DEST / "sync_state.json"
    state = load_json(state_path, {"artifacts": {}, "releases": {}})
    log(f"start repo={args.repo} dest={DEST} dry_run={args.dry_run}")

    failed = 0
    try:
        if not args.releases_only:
            download_artifacts(args.repo, state, args.limit, args.dry_run)
        if not args.artifacts_only:
            download_releases(args.repo, state, args.dry_run)
    except Exception as exc:
        failed = 1
        log(f"ERROR: {type(exc).__name__}: {exc}")

    if not args.dry_run:
        state["updated_at"] = now_utc()
        save_json(state_path, state)
        write_index(args.repo, state)
    log(f"done failed={failed}")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())

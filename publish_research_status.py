#!/usr/bin/env python3
"""Publish research progress to gh-pages/research_status.json.

The dashboard fetches this file (same origin) and renders the research progress
panel. Runs from the local daily pipeline; the cloud engine's self-publish only
touches index.html, so this file is preserved.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from entry_model_v2 import load_tokens  # noqa: E402
from resolve_research import DEFAULT_ARCHIVE  # noqa: E402

DEFAULT_REPO = "designeryork/polymarket-quant-v2"
BRANCH = "gh-pages"
REMOTE_PATH = "research_status.json"


def gh_token() -> str:
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    except Exception:
        return ""


def api(method: str, repo: str, path: str, payload: dict | None = None) -> dict:
    token = gh_token()
    if not token:
        raise RuntimeError("gh auth token unavailable")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "paper-v2-research-status",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def build_status(archive: Path, target: int) -> dict:
    samples, stats = load_tokens(archive)
    variants = stats["variants"]
    v1_resolved = int(variants.get("v1", 0))
    v2_resolved = int(variants.get("paper_v2", 0)) + int(variants.get("v2", 0))
    pending: dict[str, int | None] = {}
    for variant in ("v1", "v2"):
        path = archive / variant / "resolutions" / f"summary_{variant}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                pending[variant] = data.get("pending_unresolved")
            except Exception:
                pass
    resolved = len(samples)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "resolved_unique": resolved,
        "target": target,
        "percent": round(resolved / target * 100, 1) if target > 0 else 0.0,
        "v1_resolved": v1_resolved,
        "v2_resolved": v2_resolved,
        "eval_records": stats["eval_records"],
        "pending_unresolved": pending,
        "gate_reached": resolved >= target,
        "note": (
            "Entry model frozen. Keep paper v2 running (ALLOW_NEW_ENTRIES=true). "
            "Do not tune alpha/beta/thresholds on the current sample."
        ),
    }


def publish(repo: str, status: dict) -> None:
    content = json.dumps(status, ensure_ascii=False, indent=2) + "\n"
    sha = None
    try:
        existing = api("GET", repo, f"/contents/{REMOTE_PATH}?ref={BRANCH}")
        sha = existing.get("sha")
    except Exception:
        sha = None
    payload = {
        "message": f"update research status {status['generated_at']}",
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    api("PUT", repo, f"/contents/{REMOTE_PATH}", payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish research progress to gh-pages")
    parser.add_argument("--archive", default=DEFAULT_ARCHIVE)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--target", type=int, default=1000)
    parser.add_argument("--no-publish", action="store_true")
    args = parser.parse_args()

    archive = Path(args.archive)
    status = build_status(archive, args.target)
    local_path = archive / "research_status.json"
    local_path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)

    if args.no_publish:
        return 0
    try:
        publish(args.repo, status)
        print(f"[publish_research_status] published to {args.repo}:{BRANCH}/{REMOTE_PATH}", flush=True)
        return 0
    except Exception as exc:
        print(f"[publish_research_status] publish failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

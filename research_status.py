#!/usr/bin/env python3
"""Print paper-v2 research progress toward the 1,000-resolved-token gate."""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from entry_model_v2 import load_tokens  # noqa: E402
from resolve_research import DEFAULT_ARCHIVE  # noqa: E402

TARGET = 1000


def main() -> int:
    archive = Path(DEFAULT_ARCHIVE)
    samples, stats = load_tokens(archive)
    resolved = len(samples)
    percent = resolved / TARGET * 100 if TARGET else 0.0
    print(f"resolved unique tokens: {resolved} / {TARGET} ({percent:.1f}%)")
    print(f"resolved by variant: {dict(stats['variants'])}")
    print(f"EVAL records seen: {stats['eval_records']}")
    print()
    for variant in ("v1", "v2"):
        summary = archive / variant / "resolutions" / f"summary_{variant}.json"
        if summary.exists():
            try:
                data = json.loads(summary.read_text(encoding="utf-8"))
                print(
                    f"{variant}: unique_tokens={data.get('unique_tokens')} "
                    f"already_resolved={data.get('already_resolved')} "
                    f"pending={data.get('pending_unresolved')} "
                    f"rotation={data.get('rotation_next_offset')}"
                )
            except Exception:
                pass
    print()
    print("reports:")
    for path in (
        archive / "reports" / "ab_2026-11-07.md",
        archive / "models" / "entry_model_v2_2026-11-07.md",
    ):
        print(f"  {path}  exists={path.exists()}")
    print()
    if resolved >= TARGET:
        print("GATE REACHED: run a new pre-registered entry validation (time split / out-of-sample).")
    else:
        print(f"Keep paper v2 running (ALLOW_NEW_ENTRIES=true). {TARGET - resolved} tokens to go.")
        print("Do not tune alpha/beta/thresholds on the current sample.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

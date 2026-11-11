#!/usr/bin/env python3
"""Evaluate the pre-registered flow-shadow hypothesis after enough resolutions.

Read-only. It does not touch the engine or any ledger.

Pre-registered rule (see Docs/062):
    At the first snapshot of a token with:
      * 2h <= hours_left <= 168h
      * 15m public trades >= 10
      * |15m YES-equivalent flow ratio| >= 0.5
      * entry-side spread <= 0.05
      * entry-side depth within 2c >= $20
      * entry price in [0.10, 0.90]
    buy the flow side at the ask and hold to resolution.

Primary test:
    After >=300 resolved tokens, the mean net edge
    (win - entry ask - fee) must have a bootstrap 95% CI lower bound > 0.
    A pass only permits forward paper/shadow, never live trading.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

from resolve_research import DEFAULT_ARCHIVE, build_session, query_resolution

DEFAULT_DATA = Path(DEFAULT_ARCHIVE) / "flow_shadow" / "observations"
DEFAULT_CACHE = Path(DEFAULT_ARCHIVE) / "flow_shadow" / "resolutions.jsonl"
DEFAULT_REPORTS = Path(DEFAULT_ARCHIVE) / "reports"
MIN_TRADES = 10
MIN_FLOW = 0.5
MAX_SPREAD = 0.05
MIN_DEPTH = 20.0
MIN_PRICE = 0.10
MAX_PRICE = 0.90
MIN_HOURS = 2.0
MAX_HOURS = 168.0
TARGET_RESOLVED = 300


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_rows(data_dir: Path) -> list[dict]:
    rows = []
    for path in sorted(data_dir.rglob("*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("record_type") == "FLOW_SHADOW":
                    rows.append(row)
        except OSError:
            continue
    return rows


def qualifies(row: dict) -> Optional[dict]:
    flow = (row.get("flow") or {}).get("15m") or {}
    ratio = flow.get("flow_ratio")
    if ratio is None or abs(float(ratio)) < MIN_FLOW:
        return None
    if int(flow.get("trade_count") or 0) < MIN_TRADES:
        return None
    if not (MIN_HOURS <= float(row.get("hours_left") or 0) <= MAX_HOURS):
        return None
    long_yes = float(ratio) > 0
    entry = float((row.get("yes_ask") if long_yes else row.get("no_ask")) or 0)
    spread = float((row.get("yes_spread") if long_yes else row.get("no_spread")) or 0)
    depth = float((row.get("yes_ask_depth_usd") if long_yes else row.get("no_ask_depth_usd")) or 0)
    fee = float((row.get("fee_yes_ask") if long_yes else row.get("fee_no_ask")) or 0)
    if not (MIN_PRICE <= entry <= MAX_PRICE):
        return None
    if spread > MAX_SPREAD:
        return None
    if depth < MIN_DEPTH:
        return None
    return {
        "side": "YES" if long_yes else "NO",
        "entry": entry,
        "spread": spread,
        "depth": depth,
        "fee": fee,
        "flow_ratio": float(ratio),
        "trade_count": int(flow.get("trade_count") or 0),
    }


def first_qualifying(rows: list[dict]) -> dict[str, dict]:
    selected: dict[str, dict] = {}
    for row in sorted(rows, key=lambda item: str(item.get("observed_at") or "")):
        token = str(row.get("yes_token_id") or "")
        if not token or token in selected:
            continue
        setup = qualifies(row)
        if setup is None:
            continue
        selected[token] = {**row, **setup}
    return selected


def load_cache(path: Path) -> dict[str, int]:
    cache: dict[str, int] = {}
    if not path.exists():
        return cache
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("outcome") in (0, 1):
            cache[str(record.get("token_id"))] = int(record["outcome"])
    return cache


def resolve_all(samples: dict[str, dict], cache_path: Path) -> tuple[dict[str, int], dict[str, str]]:
    cache = load_cache(cache_path)
    session = build_session()
    statuses: dict[str, str] = {}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "a", encoding="utf-8") as handle:
        for token, row in samples.items():
            if token in cache:
                statuses[token] = "cached"
                continue
            outcome, source = query_resolution(
                session,
                {
                    "token_id": token,
                    "event_id": row.get("condition_id"),
                    "outcome_label": "Yes",
                },
            )
            statuses[token] = source
            if outcome in (0, 1):
                cache[token] = int(outcome)
                handle.write(
                    json.dumps(
                        {
                            "token_id": token,
                            "condition_id": row.get("condition_id"),
                            "outcome": int(outcome),
                            "source": source,
                            "resolved_at": now_utc(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                handle.flush()
    return cache, statuses


def bootstrap_mean(values: np.ndarray, bootstrap: int = 5000) -> list[float]:
    if len(values) == 0:
        return [0.0, 0.0, 0.0]
    rng = np.random.default_rng(71)
    means = [float(np.mean(values[rng.integers(0, len(values), len(values))])) for _ in range(bootstrap)]
    low, high = np.percentile(means, [2.5, 97.5])
    return [round(float(np.mean(values)), 6), round(float(low), 6), round(float(high), 6)]


def summarize(group: list[dict]) -> dict:
    if not group:
        return {"n": 0}
    y = np.array([row["win"] for row in group], dtype=float)
    entry = np.array([row["entry"] for row in group], dtype=float)
    fee = np.array([row["fee"] for row in group], dtype=float)
    edge = y - entry - fee
    return {
        "n": len(group),
        "hit_rate": round(float(np.mean(y)), 4),
        "mean_entry": round(float(np.mean(entry)), 4),
        "mean_edge": round(float(np.mean(edge)), 6),
        "edge_ci95": bootstrap_mean(edge),
        "mean_roi": round(float(np.mean((y - entry - fee) / entry)), 4),
    }


def render(report: dict) -> str:
    lines = [
        f"# Flow Shadow Report · {report['date']}",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- observations: **{report['observations']}**",
        f"- unique tokens: **{report['unique_tokens']}**",
        f"- first qualifying: **{report['qualifying']}**",
        f"- resolved / pending: **{report['resolved']} / {report['pending']}**",
        "- **Read-only shadow; no engine or ledger changes.**",
        "",
        "## Pre-registered rule",
        "",
        "First snapshot per token; 2h ≤ horizon ≤ 168h; 15m trades ≥ 10; "
        "|flow ratio| ≥ 0.5; entry-side spread ≤ 0.05; entry-side depth ≥ $20; "
        "entry price 0.10–0.90; buy flow side at ask; hold to resolution.",
        "",
        "## Result (resolved)",
        "",
    ]
    if report["resolved"] == 0:
        lines += ["No resolved qualifying tokens yet.", ""]
    else:
        lines += [
            "| n | hit | mean entry | mean net edge | edge 95% CI | mean ROI |",
            "|---:|---:|---:|---:|---|---:|",
            f"| {report['result']['n']} | {report['result']['hit_rate']:.3f} | "
            f"{report['result']['mean_entry']:.3f} | {report['result']['mean_edge']:+.4f} | "
            f"[{report['result']['edge_ci95'][1]:+.4f}, {report['result']['edge_ci95'][2]:+.4f}] | "
            f"{report['result']['mean_roi']:+.3f} |",
            "",
        ]
    lines += ["## By side", "", "| side | n | hit | mean edge |", "|---|---:|---:|---:|"]
    for side, row in sorted(report["by_side"].items()):
        lines.append(f"| {side} | {row['n']} | {row.get('hit_rate', 0):.3f} | {row.get('mean_edge', 0):+.4f} |")
    lines += ["", "## By category", "", "| category | n | hit | mean edge |", "|---|---:|---:|---:|"]
    for category, row in sorted(report["by_category"].items(), key=lambda item: -item[1]["n"]):
        lines.append(f"| {category} | {row['n']} | {row.get('hit_rate', 0):.3f} | {row.get('mean_edge', 0):+.4f} |")
    lines += [
        "",
        "## Decision",
        "",
        report["decision"],
        "",
        "## Caveats",
        "",
        "- One first-qualifying snapshot per token; no backfill, no queue/partial-fill simulation.",
        "- Net edge subtracts the recorded fee per share, not gas or market impact.",
        "- A pass is only a license for forward paper/shadow; never for live trading.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate pre-registered flow-shadow hypothesis")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    parser.add_argument("--out-dir", default=str(DEFAULT_REPORTS))
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--bootstrap", type=int, default=5000)
    args = parser.parse_args()

    rows = load_rows(Path(args.data_dir))
    samples = first_qualifying(rows)
    cache, statuses = resolve_all(samples, Path(args.cache))
    resolved: list[dict] = []
    pending = 0
    for token, row in samples.items():
        outcome = cache.get(token)
        if outcome not in (0, 1):
            pending += 1
            continue
        win = outcome if row["side"] == "YES" else 1 - outcome
        resolved.append({**row, "win": win})

    by_side: dict[str, list[dict]] = defaultdict(list)
    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in resolved:
        by_side[row["side"]].append(row)
        by_category[row.get("category") or "其他"].append(row)

    result = summarize(resolved)
    resolved_sorted = sorted(resolved, key=lambda row: str(row.get("observed_at") or ""))
    half = len(resolved_sorted) // 2
    validation = summarize(resolved_sorted[:half])
    test = summarize(resolved_sorted[half:])

    if len(resolved) < TARGET_RESOLVED:
        decision = (
            f"INSUFFICIENT: {len(resolved)} resolved < {TARGET_RESOLVED}. "
            "Keep collecting; do not tune thresholds."
        )
    elif test.get("n", 0) and test["edge_ci95"][1] > 0:
        decision = (
            "PASS (forward shadow only): test-half mean net edge CI lower > 0. "
            "Next step is a fixed-rule forward paper test; never live."
        )
    else:
        decision = (
            "NO-GO: test-half net edge CI lower is not > 0. "
            "Close the flow-shadow hypothesis; do not tune on the same data."
        )

    report = {
        "generated_at": now_utc(),
        "date": args.date,
        "observations": len(rows),
        "unique_tokens": len(samples),
        "qualifying": len(samples),
        "resolved": len(resolved),
        "pending": pending,
        "resolution_statuses": dict(Counter(statuses.values())),
        "result": result,
        "validation_half": validation,
        "test_half": test,
        "by_side": {side: summarize(group) for side, group in by_side.items()},
        "by_category": {category: summarize(group) for category, group in by_category.items()},
        "decision": decision,
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"flow_shadow_{args.date}.json"
    md_path = out_dir / f"flow_shadow_{args.date}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "observations": len(rows),
                "qualifying": len(samples),
                "resolved": len(resolved),
                "pending": pending,
                "decision": decision,
                "report_md": str(md_path),
                "report_json": str(json_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

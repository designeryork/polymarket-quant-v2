#!/usr/bin/env python3
"""A/B observation report: v1 vs v2.

Phase 2.3 — observation only. It joins each variant's EVAL records to its
resolutions, compares calibration/expectancy, and (optionally) reads the cloud
paper ledgers for trade-level metrics.

Usage:
    python ab_report.py --period 2026-11-07
    python ab_report.py --period 2026-11-07 --no-fetch-ledgers
    python ab_report.py --v1-portfolio path --v2-portfolio path
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from daily_metrics import (  # noqa: E402
    DEFAULT_ARCHIVE,
    build_samples,
    calibration_bins,
    fmt,
    group_report,
    load_evals,
    load_resolutions,
    summarize,
)

REPOS = {
    "v1": "designeryork/polymarket-quant",
    "v2": "designeryork/polymarket-quant-v2",
}
SNAPSHOT_REF = "state-snapshot"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def gh_content(repo: str, path: str) -> Optional[str]:
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/contents/{path}?ref={SNAPSHOT_REF}",
                "--jq",
                ".content",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            return None
        return base64.b64decode(result.stdout.replace("\n", "")).decode("utf-8")
    except Exception:
        return None


def fetch_ledger(variant: str) -> Optional[dict]:
    text = gh_content(REPOS[variant], "state/portfolio_paper.json")
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def fetch_equity(variant: str) -> list[dict]:
    text = gh_content(REPOS[variant], "state/equity_history_paper.jsonl")
    if not text:
        return []
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def max_drawdown(equity_rows: list[dict]) -> Optional[float]:
    points = []
    for row in equity_rows:
        try:
            points.append(float(row.get("equity")))
        except (TypeError, ValueError):
            continue
    peak = 0.0
    worst = 0.0
    for value in points:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return round(worst, 6) if points else None


def trade_metrics(portfolio: Optional[dict], equity_rows: list[dict]) -> dict:
    if not portfolio:
        return {"provided": False}
    trades = portfolio.get("closed_trades") or []
    pnls = []
    holding_hours = []
    right_tail = []
    for trade in trades:
        try:
            pnls.append(float(trade.get("realized_pnl_usdc") or 0.0))
        except (TypeError, ValueError):
            pass
        opened = parse_iso(trade.get("opened_at"))
        closed = parse_iso(trade.get("closed_at"))
        if opened and closed:
            holding_hours.append((closed - opened).total_seconds() / 3600.0)
        try:
            entry = float(trade.get("entry_price"))
            exit_price = float(trade.get("exit_price_bid"))
            peak = float(trade.get("peak_bid"))
        except (TypeError, ValueError):
            continue
        if peak > entry and exit_price > entry:
            right_tail.append((exit_price - entry) / (peak - entry))

    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    total = sum(pnls)
    top3 = sum(sorted(pnls, reverse=True)[:3])
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    payoff = (avg_win / abs(avg_loss)) if avg_win is not None and avg_loss not in (None, 0) else None
    return {
        "provided": True,
        "closed_trades": len(pnls),
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
        "avg_win_usdc": round(avg_win, 4) if avg_win is not None else None,
        "avg_loss_usdc": round(avg_loss, 4) if avg_loss is not None else None,
        "payoff_ratio": round(payoff, 4) if payoff is not None else None,
        "total_realized_usdc": round(total, 4),
        "expectancy_usdc": round(total / len(pnls), 4) if pnls else None,
        "top3_realized_usdc": round(top3, 4),
        "top3_share_of_profit": round(top3 / total, 4) if total > 0 else None,
        "avg_holding_hours": round(sum(holding_hours) / len(holding_hours), 2) if holding_hours else None,
        "right_tail_capture": round(sum(right_tail) / len(right_tail), 4) if right_tail else None,
        "max_drawdown": max_drawdown(equity_rows),
        "initial_equity": portfolio.get("initial_capital_usdc"),
        "current_equity": portfolio.get("total_equity_usdc"),
    }


def load_variant(variant: str, root: Path, start: Optional[str], end: Optional[str]) -> dict:
    evals = load_evals(root)
    for row in evals:
        row["variant"] = row.get("variant") or variant
        row["strategy_version"] = row.get("strategy_version") or f"{variant}-default"
    if start:
        evals = [row for row in evals if str(row.get("ts_utc") or "") >= start]
    if end:
        evals = [row for row in evals if str(row.get("ts_utc") or "") <= end]
    resolutions = load_resolutions(root)
    samples = build_samples(evals, resolutions)
    return {"evals": evals, "resolutions": resolutions, "samples": samples, "root": root}


def window(evals: list[dict]) -> dict:
    timestamps = [str(row.get("ts_utc")) for row in evals if row.get("ts_utc")]
    if not timestamps:
        return {"first": None, "last": None}
    return {"first": min(timestamps), "last": max(timestamps)}


def report_entry(stats: dict, bootstrap: int) -> dict:
    samples = stats["samples"]
    evals = stats["evals"]
    resolutions = stats["resolutions"]
    unique_tokens = len({row["token_id"] for row in evals})
    return {
        "eval_records": len(evals),
        "unique_tokens": unique_tokens,
        "resolved_tokens": len(resolutions),
        "joined_samples": len(samples),
        "unresolved_tokens": max(0, unique_tokens - len(resolutions)),
        "window": window(evals),
        "headline": summarize(samples, bootstrap),
        "calibration": calibration_bins(samples),
        "by_stage": group_report(samples, "strategy_version", bootstrap),
        "by_gate": group_report(samples, "gate_result", bootstrap),
    }


def markdown(report: dict) -> str:
    lines = [
        f"# A/B Observation Report · {report['period']}",
        "",
        f"- generated_at: `{report['generated_at']}`",
        "- **Observation only. Do not select a winner from this report.**",
        "",
        "## Validity (read first)",
        "",
        "| item | v1 | v2 |",
        "|---|---:|---:|",
    ]
    for key in ("eval_records", "unique_tokens", "resolved_tokens", "joined_samples", "unresolved_tokens"):
        lines.append(f"| {key} | {report['v1'][key]} | {report['v2'][key]} |")
    w1 = report["v1"]["window"]
    w2 = report["v2"]["window"]
    lines.append(f"| eval window | {w1['first']} → {w1['last']} | {w2['first']} → {w2['last']} |")
    lines += [
        "",
        "**Read these caveats before comparing:**",
        "",
        "- v1/v2 are not randomized; they ran different windows and market sets.",
        "- Resolved samples are biased toward short-horizon events; unresolved tokens dominate.",
        "- The entry model has not passed calibration (see Docs/055); claimed edge is not evidence.",
        "- If either side has <100 resolved tokens, treat the comparison as INSUFFICIENT.",
        "",
        "## EVAL-level comparison",
        "",
        "| metric | v1 | v2 |",
        "|---|---:|---:|",
    ]
    keys = [
        "n",
        "unique_tokens",
        "brier_model",
        "brier_market",
        "diff",
        "diff_ci95",
        "hit_rate",
        "claimed_edge",
        "realized_edge",
        "mean_pnl_per_dollar",
        "pnl_ci95",
    ]
    for key in keys:
        lines.append(
            f"| {key} | {fmt(report['v1']['headline'].get(key))} | "
            f"{fmt(report['v2']['headline'].get(key))} |"
        )

    lines += ["", "## By stage / strategy_version", "", "| stage | n | Brier model | Brier market | hit | realized_edge |", "|---|---:|---:|---:|---:|---:|"]
    for variant in ("v1", "v2"):
        for row in report[variant]["by_stage"]:
            lines.append(
                f"| {row['group']} | {row['n']} | {fmt(row.get('brier_model'))} | "
                f"{fmt(row.get('brier_market'))} | {fmt(row.get('hit_rate'))} | "
                f"{fmt(row.get('realized_edge'))} |"
            )

    for variant in ("v1", "v2"):
        lines += ["", f"## Calibration · {variant}", "", "| bin | n | mean_pred | observed | Brier model | Brier market |", "|---|---:|---:|---:|---:|---:|"]
        for row in report[variant]["calibration"]:
            lines.append(
                f"| {row['range'][0]:.1f}-{row['range'][1]:.1f} | {row['n']} | "
                f"{row['mean_prediction']:.4f} | {row['observed_frequency']:.4f} | "
                f"{row['brier_model']:.6f} | {row['brier_market']:.6f} |"
            )

    lines += ["", "## Trade-level (cloud paper ledgers)", "", "| metric | v1 | v2 |", "|---|---:|---:|"]
    trade_keys = [
        "provided",
        "closed_trades",
        "win_rate",
        "avg_win_usdc",
        "avg_loss_usdc",
        "payoff_ratio",
        "total_realized_usdc",
        "expectancy_usdc",
        "top3_share_of_profit",
        "avg_holding_hours",
        "right_tail_capture",
        "max_drawdown",
        "initial_equity",
        "current_equity",
    ]
    for key in trade_keys:
        lines.append(
            f"| {key} | {fmt(report['v1']['trades'].get(key))} | "
            f"{fmt(report['v2']['trades'].get(key))} |"
        )

    lines += [
        "",
        "## Verdict",
        "",
        "- This report is a **measurement frame**, not a winner selection.",
        "- Both sides' claimed edges must be treated as unvalidated until Brier/calibration passes.",
        "- Do not tune exits or size based on this table.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="A/B observation report v1 vs v2")
    parser.add_argument("--archive", default=DEFAULT_ARCHIVE)
    parser.add_argument("--period", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--start", default="", help="ISO date/time lower bound for EVAL ts_utc")
    parser.add_argument("--end", default="", help="ISO date/time upper bound for EVAL ts_utc")
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--fetch-ledgers", dest="fetch_ledgers", action="store_true", default=True)
    parser.add_argument("--no-fetch-ledgers", dest="fetch_ledgers", action="store_false")
    parser.add_argument("--v1-portfolio", default="")
    parser.add_argument("--v2-portfolio", default="")
    parser.add_argument("--v1-equity", default="")
    parser.add_argument("--v2-equity", default="")
    args = parser.parse_args()

    archive = Path(args.archive)
    stats = {}
    for variant in ("v1", "v2"):
        root = archive / variant
        if not root.exists():
            print(f"[ab_report] skip {variant}: {root} not found", flush=True)
            continue
        try:
            stats[variant] = load_variant(variant, root, args.start or None, args.end or None)
        except Exception as exc:
            print(f"[ab_report] {variant} load failed: {exc}", flush=True)

    if "v1" not in stats or "v2" not in stats:
        print("[ab_report] need both v1 and v2 archives; skip", flush=True)
        return 0

    v1_report = report_entry(stats["v1"], args.bootstrap)
    v2_report = report_entry(stats["v2"], args.bootstrap)

    def load_portfolio(variant: str, path: str) -> Optional[dict]:
        if path:
            try:
                return json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[ab_report] {variant} portfolio load failed: {exc}", flush=True)
                return None
        if args.fetch_ledgers:
            return fetch_ledger(variant)
        return None

    def load_equity(variant: str, path: str) -> list[dict]:
        if path:
            rows = []
            try:
                for line in Path(path).read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        rows.append(json.loads(line))
            except Exception as exc:
                print(f"[ab_report] {variant} equity load failed: {exc}", flush=True)
            return rows
        if args.fetch_ledgers:
            return fetch_equity(variant)
        return []

    v1_trades = trade_metrics(load_portfolio("v1", args.v1_portfolio), load_equity("v1", args.v1_equity))
    v2_trades = trade_metrics(load_portfolio("v2", args.v2_portfolio), load_equity("v2", args.v2_equity))

    report = {
        "generated_at": now_utc(),
        "period": args.period,
        "archive": str(archive),
        "v1": {**v1_report, "trades": v1_trades},
        "v2": {**v2_report, "trades": v2_trades},
        "valid_for_selection": False,
    }

    out_dir = archive / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"ab_{args.period}.md"
    json_path = out_dir / f"ab_{args.period}.json"
    md_path.write_text(markdown(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "period": args.period,
                "v1": {
                    "resolved_tokens": v1_report["resolved_tokens"],
                    "joined_samples": v1_report["joined_samples"],
                    "headline": v1_report["headline"],
                    "trades": v1_trades,
                },
                "v2": {
                    "resolved_tokens": v2_report["resolved_tokens"],
                    "joined_samples": v2_report["joined_samples"],
                    "headline": v2_report["headline"],
                    "trades": v2_trades,
                },
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

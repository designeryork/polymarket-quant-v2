#!/usr/bin/env python3
"""Daily calibration / metrics report for paper-v2 research archives.

Phase 2.2 — measurement only. Reads the local archive + resolutions.jsonl,
joins every EVAL to its settlement, and writes Markdown/JSON reports.

Usage:
    python daily_metrics.py --variant v2
    python daily_metrics.py --variant all --bootstrap 2000
    python daily_metrics.py --variant v2 --portfolio path/to/portfolio_paper.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from resolve_research import DEFAULT_ARCHIVE, iter_text_sources  # noqa: E402

try:
    from quant_engine import market_category  # type: ignore
except Exception:  # pragma: no cover - fallback for standalone runs

    def market_category(question: Optional[str]) -> str:
        return "其他"


PRICE_BANDS = (
    (0.00, 0.15),
    (0.15, 0.25),
    (0.25, 0.35),
    (0.35, 0.50),
    (0.50, 0.65),
    (0.65, 0.75),
    (0.75, 0.85),
    (0.85, 1.00),
)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_evals(root: Path) -> list[dict]:
    """All unique EVAL records (artifact + release, deduped by token+ts)."""
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for _source, text in iter_text_sources(root):
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("record_type") != "EVAL":
                continue
            token_id = str(record.get("token_id") or "")
            ts = str(record.get("ts_utc") or "")
            key = (token_id, ts)
            if key in seen:
                continue
            seen.add(key)
            p_est = record.get("p_est")
            market = record.get("signal_price")
            if market is None:
                market = record.get("execution_price")
            try:
                p_est = float(p_est)
                market = float(market)
            except (TypeError, ValueError):
                continue
            if not (0.0 < p_est < 1.0 and 0.0 < market < 1.0):
                continue
            rows.append(
                {
                    "token_id": token_id,
                    "event_id": record.get("event_id"),
                    "question": str(record.get("question") or ""),
                    "p_est": p_est,
                    "market_price": market,
                    "gate_result": str(record.get("gate_result") or "unknown"),
                    "metrics": record.get("metrics") or {},
                    "ts_utc": ts,
                    "variant": record.get("variant"),
                    "strategy_version": record.get("strategy_version"),
                }
            )
    return rows


def load_resolutions(root: Path) -> dict[str, int]:
    path = root / "resolutions" / "resolutions.jsonl"
    result: dict[str, int] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("outcome") in (0, 1):
            result[str(record.get("token_id"))] = int(record["outcome"])
    return result


def build_samples(evals: list[dict], resolutions: dict[str, int]) -> list[dict]:
    samples = []
    for row in evals:
        outcome = resolutions.get(row["token_id"])
        if outcome not in (0, 1):
            continue
        samples.append({**row, "outcome": int(outcome)})
    return samples


def cluster_groups(samples: list[dict]) -> tuple[list[str], dict[str, list[int]]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for index, sample in enumerate(samples):
        groups[sample["token_id"]].append(index)
    return sorted(groups), groups


def brier_ci(samples: list[dict], bootstrap: int, seed: int = 42) -> dict:
    if not samples:
        return {}
    p = np.array([s["p_est"] for s in samples], dtype=float)
    m = np.array([s["market_price"] for s in samples], dtype=float)
    y = np.array([s["outcome"] for s in samples], dtype=float)
    brier_model = float(np.mean((p - y) ** 2))
    brier_market = float(np.mean((m - y) ** 2))
    diff = brier_model - brier_market
    tokens, groups = cluster_groups(samples)
    rng = np.random.default_rng(seed)
    if len(tokens) >= 5 and bootstrap > 0:
        values = []
        for _ in range(bootstrap):
            chosen = rng.choice(tokens, size=len(tokens), replace=True)
            idx = np.concatenate([groups[token] for token in chosen])
            values.append(
                float(np.mean((p[idx] - y[idx]) ** 2) - np.mean((m[idx] - y[idx]) ** 2))
            )
        low, high = np.percentile(values, [2.5, 97.5])
    else:
        low = high = None
    return {
        "brier_model": round(brier_model, 6),
        "brier_market": round(brier_market, 6),
        "diff": round(diff, 6),
        "diff_ci95": [round(float(low), 6), round(float(high), 6)] if low is not None else None,
        "ci_cluster": "token",
        "cluster_tokens": len(tokens),
    }


def ev_ci(samples: list[dict], bootstrap: int, seed: int = 43) -> dict:
    if not samples:
        return {}
    m = np.array([s["market_price"] for s in samples], dtype=float)
    y = np.array([s["outcome"] for s in samples], dtype=float)
    pnl = y / m - 1.0
    mean_pnl = float(np.mean(pnl))
    median_pnl = float(np.median(pnl))
    tokens, groups = cluster_groups(samples)
    rng = np.random.default_rng(seed)
    if len(tokens) >= 5 and bootstrap > 0:
        values = []
        for _ in range(bootstrap):
            chosen = rng.choice(tokens, size=len(tokens), replace=True)
            idx = np.concatenate([groups[token] for token in chosen])
            values.append(float(np.mean(pnl[idx])))
        low, high = np.percentile(values, [2.5, 97.5])
    else:
        low = high = None
    return {
        "mean_pnl_per_dollar": round(mean_pnl, 6),
        "median_pnl_per_dollar": round(median_pnl, 6),
        "pnl_ci95": [round(float(low), 6), round(float(high), 6)] if low is not None else None,
        "pnl_ci_cluster": "token",
    }


def summarize(samples: list[dict], bootstrap: int) -> dict:
    if not samples:
        return {"n": 0}
    p = np.array([s["p_est"] for s in samples], dtype=float)
    m = np.array([s["market_price"] for s in samples], dtype=float)
    y = np.array([s["outcome"] for s in samples], dtype=float)
    brier = brier_ci(samples, bootstrap)
    pnl = ev_ci(samples, bootstrap)
    return {
        "n": len(samples),
        "unique_tokens": len({s["token_id"] for s in samples}),
        "mean_p_est": round(float(np.mean(p)), 6),
        "mean_market_price": round(float(np.mean(m)), 6),
        "hit_rate": round(float(np.mean(y)), 6),
        "claimed_edge": round(float(np.mean(p - m)), 6),
        "realized_edge": round(float(np.mean(y - m)), 6),
        **brier,
        **pnl,
    }


def calibration_bins(samples: list[dict]) -> list[dict]:
    bins: dict[int, list[dict]] = defaultdict(list)
    for s in samples:
        index = min(9, max(0, int(s["p_est"] * 10)))
        bins[index].append(s)
    rows = []
    for index in sorted(bins):
        group = bins[index]
        p = np.array([s["p_est"] for s in group], dtype=float)
        m = np.array([s["market_price"] for s in group], dtype=float)
        y = np.array([s["outcome"] for s in group], dtype=float)
        rows.append(
            {
                "range": [index / 10, (index + 1) / 10],
                "n": len(group),
                "mean_prediction": round(float(np.mean(p)), 4),
                "observed_frequency": round(float(np.mean(y)), 4),
                "brier_model": round(float(np.mean((p - y) ** 2)), 6),
                "brier_market": round(float(np.mean((m - y) ** 2)), 6),
            }
        )
    return rows


def band_label(low: float, high: float) -> str:
    return f"{low:.2f}-{high:.2f}"


def group_report(samples: list[dict], key: str, bootstrap: int) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        groups[str(s.get(key) or "unknown")].append(s)
    rows = []
    for name in sorted(groups):
        summary = summarize(groups[name], bootstrap)
        rows.append({"group": name, **summary})
    return rows


def price_band_report(samples: list[dict], bootstrap: int) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        price = s["market_price"]
        label = "outside"
        for low, high in PRICE_BANDS:
            if low <= price < high:
                label = band_label(low, high)
                break
        groups[label].append(s)
    rows = []
    for name in sorted(groups):
        summary = summarize(groups[name], bootstrap)
        rows.append({"band": name, **summary})
    return rows


def category_report(samples: list[dict], bootstrap: int) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        groups[market_category(s.get("question"))].append(s)
    rows = []
    for name in sorted(groups):
        summary = summarize(groups[name], bootstrap)
        rows.append({"category": name, **summary})
    return rows


def concentration(portfolio_path: Optional[str]) -> dict:
    if not portfolio_path:
        return {"provided": False}
    path = Path(portfolio_path)
    if not path.exists():
        return {"provided": False, "error": f"not found: {path}"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"provided": False, "error": str(exc)}
    trades = data.get("closed_trades") or []
    pnls = [float(t.get("realized_pnl_usdc") or 0.0) for t in trades]
    total = sum(pnls)
    top3 = sum(sorted(pnls, reverse=True)[:3])
    return {
        "provided": True,
        "path": str(path),
        "closed_trades": len(pnls),
        "total_realized_usdc": round(total, 6),
        "top3_realized_usdc": round(top3, 6),
        "top3_share_of_profit": round(top3 / total, 4) if total > 0 else None,
    }


def fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:+.4f}" if abs(value) < 1 else f"{value:.4f}"
    return str(value)


def write_markdown(report: dict, path: Path) -> None:
    lines = [
        f"# Paper {report['variant']} · Daily Metrics · {report['date']}",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- archive: `{report['archive']}`",
        "",
        "## Coverage",
        "",
        "| metric | value |",
        "|---|---:|",
    ]
    for key, value in report["coverage"].items():
        lines.append(f"| {key} | {fmt(value)} |")

    headline = report["headline"]
    lines += ["", "## Headline (resolved samples; token-clustered CI)", "", "| metric | value |", "|---|---:|"]
    for key, value in headline.items():
        if key in ("n", "unique_tokens"):
            lines.append(f"| {key} | {value} |")
        else:
            lines.append(f"| {key} | {fmt(value)} |")

    lines += ["", "## Calibration by p_est", "", "| bin | n | mean_pred | observed | Brier model | Brier market |", "|---|---:|---:|---:|---:|---:|"]
    for row in report["calibration"]:
        lines.append(
            f"| {row['range'][0]:.1f}-{row['range'][1]:.1f} | {row['n']} | "
            f"{row['mean_prediction']:.4f} | {row['observed_frequency']:.4f} | "
            f"{row['brier_model']:.6f} | {row['brier_market']:.6f} |"
        )

    lines += ["", "## By price band", "", "| band | n | mean_price | hit | claimed_edge | realized_edge | Brier model | Brier market |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in report["by_price_band"]:
        lines.append(
            f"| {row['band']} | {row['n']} | {row.get('mean_market_price', 0):.4f} | "
            f"{row.get('hit_rate', 0):.4f} | {row.get('claimed_edge', 0):+.4f} | "
            f"{row.get('realized_edge', 0):+.4f} | {row.get('brier_model', 0):.6f} | "
            f"{row.get('brier_market', 0):.6f} |"
        )

    for title, key, label in (("By gate_result", "by_gate", "group"), ("By category", "by_category", "category")):
        lines += ["", f"## {title}", "", f"| {label} | n | mean_price | hit | claimed_edge | realized_edge | Brier model | Brier market |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for row in report[key]:
            lines.append(
                f"| {row[label]} | {row['n']} | {row.get('mean_market_price', 0):.4f} | "
                f"{row.get('hit_rate', 0):.4f} | {row.get('claimed_edge', 0):+.4f} | "
                f"{row.get('realized_edge', 0):+.4f} | {row.get('brier_model', 0):.6f} | "
                f"{row.get('brier_market', 0):.6f} |"
            )

    lines += ["", "## Concentration", ""]
    conc = report["concentration"]
    if conc.get("provided"):
        lines.append(f"- closed_trades: {conc['closed_trades']}")
        lines.append(f"- total realized: ${conc['total_realized_usdc']:+.2f}")
        lines.append(f"- top 3: ${conc['top3_realized_usdc']:+.2f}")
        lines.append(f"- top 3 share: {conc['top3_share_of_profit']}")
    else:
        lines.append("- portfolio not provided; run with `--portfolio path/to/portfolio_paper.json`")

    lines += [
        "",
        "## Caveats",
        "",
        "- Resolved samples are not a random sample: short-horizon events settle first.",
        "- Bootstrap resamples **unique tokens**, not individual EVALs; multiple EVALs of the same token share one outcome.",
        "- `mean_pnl_per_dollar` is the average per-$1 ROI; `realized_edge` is probability points. They differ because ROI weights low-price trades heavily.",
        "- Brier difference CI crossing 0 means no detectable difference at this sample size.",
        "- Do not tune parameters on this report; use it as a measurement gate.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_variant(variant: str, root: Path, date_label: str, bootstrap: int, portfolio: Optional[str]) -> dict:
    evals = load_evals(root)
    resolutions = load_resolutions(root)
    samples = build_samples(evals, resolutions)
    unique_tokens = len({e["token_id"] for e in evals})
    resolved_tokens = len({t for t in resolutions if t})
    coverage = {
        "eval_records": len(evals),
        "unique_tokens": unique_tokens,
        "resolved_tokens": resolved_tokens,
        "joined_samples": len(samples),
        "unresolved_tokens": max(0, unique_tokens - resolved_tokens),
    }
    report = {
        "generated_at": now_utc(),
        "variant": variant,
        "date": date_label,
        "archive": str(root),
        "coverage": coverage,
        "headline": summarize(samples, bootstrap),
        "calibration": calibration_bins(samples),
        "by_price_band": price_band_report(samples, bootstrap),
        "by_gate": group_report(samples, "gate_result", bootstrap),
        "by_category": category_report(samples, bootstrap),
        "concentration": concentration(portfolio),
    }
    out_dir = root / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"daily_{date_label}_{variant}.md"
    json_path = out_dir / f"daily_{date_label}_{variant}.json"
    write_markdown(report, md_path)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "variant": variant,
                "coverage": coverage,
                "headline": report["headline"],
                "report_md": str(md_path),
                "report_json": str(json_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily calibration report for paper-v2")
    parser.add_argument("--archive", default=DEFAULT_ARCHIVE)
    parser.add_argument("--variant", choices=["v1", "v2", "all"], default="v2")
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--portfolio", default="", help="optional portfolio JSON for top-3 concentration")
    args = parser.parse_args()

    archive = Path(args.archive)
    variants = ["v1", "v2"] if args.variant == "all" else [args.variant]
    for variant in variants:
        root = archive / variant
        if not root.exists():
            print(f"[daily_metrics] skip {variant}: {root} not found", flush=True)
            continue
        run_variant(variant, root, args.date, args.bootstrap, args.portfolio or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

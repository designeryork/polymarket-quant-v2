#!/usr/bin/env python3
"""Offline entry-model v2: empirical conditional probability with Beta shrinkage.

Phase 4.1 — measurement only. It does NOT touch quant_engine.py. It reads all
resolved EVAL tokens, builds simple empirical probability models, and evaluates
them with leave-one-token-out (LOO) predictions against the market price and
the current mechanical p_est.

Models:
    market            price (baseline)
    current_p_est     quant_engine's p_est (mechanical momentum)
    emp_band          P(win | price band), Beta-shrunk, LOO
    emp_band_roc      P(win | price band x ROC sign), shrunk to band, LOO

Usage:
    python entry_model_v2.py --date 2026-11-07
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from daily_metrics import market_category  # noqa: E402
from resolve_research import DEFAULT_ARCHIVE, iter_text_sources  # noqa: E402


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


PRICE_BANDS = (
    (0.00, 0.15, "0.00-0.15"),
    (0.15, 0.25, "0.15-0.25"),
    (0.25, 0.35, "0.25-0.35"),
    (0.35, 0.50, "0.35-0.50"),
    (0.50, 0.65, "0.50-0.65"),
    (0.65, 0.75, "0.65-0.75"),
    (0.75, 0.85, "0.75-0.85"),
    (0.85, 1.00, "0.85-1.00"),
)


def price_band(price: float) -> str:
    for low, high, label in PRICE_BANDS:
        if low <= price < high:
            return label
    return "outside"


def roc_sign(roc: float) -> str:
    return "pos" if roc > 0 else "nonpos"


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


def load_tokens(archive: Path) -> tuple[list[dict], dict]:
    """One resolved token = one sample; keep the earliest EVAL per token."""
    tokens: dict[str, dict] = {}
    stats = {"variants": Counter(), "eval_records": 0, "duplicates_skipped": 0}
    for variant in ("v1", "v2"):
        root = archive / variant
        if not root.exists():
            continue
        resolutions = load_resolutions(root)
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
                stats["eval_records"] += 1
                token_id = str(record.get("token_id") or "")
                if not token_id:
                    continue
                outcome = resolutions.get(token_id)
                if outcome not in (0, 1):
                    continue
                ts = str(record.get("ts_utc") or "")
                previous = tokens.get(token_id)
                if previous and str(previous.get("ts_utc") or "") <= ts:
                    stats["duplicates_skipped"] += 1
                    continue
                metrics = record.get("metrics") or {}
                price = record.get("signal_price")
                if price is None:
                    price = record.get("execution_price")
                try:
                    price = float(price)
                    p_est = float(record.get("p_est"))
                    roc = float(metrics.get("roc_logit_6") or 0.0)
                    atr = float(metrics.get("atr_z_14") or 0.0)
                    imb = float(metrics.get("imbalance") or 0.0)
                    spread = float(metrics.get("spread") or 0.0)
                except (TypeError, ValueError):
                    continue
                if not (0.0 < price < 1.0):
                    continue
                if not (0.0 < p_est < 1.0):
                    continue
                question = str(record.get("question") or "")
                tokens[token_id] = {
                    "token_id": token_id,
                    "variant": record.get("variant") or variant,
                    "strategy_version": record.get("strategy_version") or f"{variant}-default",
                    "ts_utc": ts,
                    "question": question,
                    "category": market_category(question),
                    "price": price,
                    "p_est": p_est,
                    "roc": roc,
                    "atr": atr,
                    "imbalance": imb,
                    "spread": spread,
                    "gate_result": str(record.get("gate_result") or "unknown"),
                    "outcome": int(outcome),
                }
                stats["variants"][record.get("variant") or variant] += 1
    return list(tokens.values()), stats


def brier(pred: dict[str, float], samples: list[dict]) -> float:
    return float(np.mean([(pred[s["token_id"]] - s["outcome"]) ** 2 for s in samples]))


def logloss(pred: dict[str, float], samples: list[dict]) -> float:
    values = []
    for s in samples:
        p = min(1 - 1e-6, max(1e-6, pred[s["token_id"]]))
        y = s["outcome"]
        values.append(-(y * math.log(p) + (1 - y) * math.log(1 - p)))
    return float(np.mean(values))


def bootstrap_diff(
    pred_a: dict[str, float],
    pred_b: dict[str, float],
    samples: list[dict],
    bootstrap: int = 2000,
    seed: int = 44,
) -> list[float]:
    a = np.array([pred_a[s["token_id"]] for s in samples], dtype=float)
    b = np.array([pred_b[s["token_id"]] for s in samples], dtype=float)
    y = np.array([s["outcome"] for s in samples], dtype=float)
    point = float(np.mean((a - y) ** 2) - np.mean((b - y) ** 2))
    rng = np.random.default_rng(seed)
    values = []
    for _ in range(bootstrap):
        idx = rng.integers(0, len(y), size=len(y))
        values.append(float(np.mean((a[idx] - y[idx]) ** 2) - np.mean((b[idx] - y[idx]) ** 2)))
    low, high = np.percentile(values, [2.5, 97.5])
    return [round(point, 6), round(float(low), 6), round(float(high), 6)]


def empirical_loo(
    samples: list[dict],
    key_fn: Callable[[dict], Any],
    parent_fn: Optional[Callable[[dict], Any]],
    alpha: float,
    beta: float,
    min_group: int,
) -> tuple[dict[str, float], dict]:
    """Leave-one-out Beta-binomial predictions with parent fallback."""
    group_n = Counter()
    group_w = Counter()
    parent_n = Counter()
    parent_w = Counter()
    total_n = 0
    total_w = 0
    for sample in samples:
        key = key_fn(sample)
        group_n[key] += 1
        group_w[key] += sample["outcome"]
        total_n += 1
        total_w += sample["outcome"]
        if parent_fn:
            parent = parent_fn(sample)
            parent_n[parent] += 1
            parent_w[parent] += sample["outcome"]

    pred: dict[str, float] = {}
    fallback_counts = Counter()
    for sample in samples:
        key = key_fn(sample)
        y = sample["outcome"]
        n = group_n[key] - 1
        w = group_w[key] - y
        if n >= min_group:
            p = (w + alpha) / (n + alpha + beta)
        elif parent_fn:
            parent = parent_fn(sample)
            pn = parent_n[parent] - 1
            pw = parent_w[parent] - y
            if pn >= min_group:
                p = (pw + alpha) / (pn + alpha + beta)
                fallback_counts["parent"] += 1
            else:
                p = (total_w - y + alpha) / (total_n - 1 + alpha + beta)
                fallback_counts["global"] += 1
        else:
            p = (total_w - y + alpha) / (total_n - 1 + alpha + beta)
            fallback_counts["global"] += 1
        pred[sample["token_id"]] = float(min(1 - 1e-6, max(1e-6, p)))
    return pred, {
        "group_sizes": {str(k): int(v) for k, v in group_n.items()},
        "fallbacks": dict(fallback_counts),
    }


def full_table(
    samples: list[dict],
    key_fn: Callable[[dict], Any],
    alpha: float,
    beta: float,
) -> list[dict]:
    n = Counter()
    w = Counter()
    for sample in samples:
        key = key_fn(sample)
        n[key] += 1
        w[key] += sample["outcome"]
    rows = []
    for key in sorted(n, key=str):
        rows.append(
            {
                "group": str(key),
                "n": n[key],
                "wins": w[key],
                "p_emp": round((w[key] + alpha) / (n[key] + alpha + beta), 6),
            }
        )
    return rows


def calibration(pred: dict[str, float], samples: list[dict]) -> list[dict]:
    bins: dict[int, list[dict]] = defaultdict(list)
    for sample in samples:
        p = pred[sample["token_id"]]
        index = min(9, max(0, int(p * 10)))
        bins[index].append(sample)
    rows = []
    for index in sorted(bins):
        group = bins[index]
        ps = np.array([pred[s["token_id"]] for s in group])
        ys = np.array([s["outcome"] for s in group])
        rows.append(
            {
                "range": [index / 10, (index + 1) / 10],
                "n": len(group),
                "mean_prediction": round(float(np.mean(ps)), 4),
                "observed_frequency": round(float(np.mean(ys)), 4),
            }
        )
    return rows


def rule_eval(
    pred: dict[str, float],
    samples: list[dict],
    thresholds: list[float],
) -> list[dict]:
    rows = []
    for threshold in thresholds:
        selected = [
            s for s in samples
            if pred[s["token_id"]] - s["price"] >= threshold
        ]
        if not selected:
            rows.append({"threshold": threshold, "n": 0})
            continue
        y = np.array([s["outcome"] for s in selected], dtype=float)
        price = np.array([s["price"] for s in selected], dtype=float)
        pnl = y / price - 1.0
        rows.append(
            {
                "threshold": threshold,
                "n": len(selected),
                "hit_rate": round(float(np.mean(y)), 4),
                "mean_price": round(float(np.mean(price)), 4),
                "realized_edge": round(float(np.mean(y - price)), 4),
                "mean_roi": round(float(np.mean(pnl)), 4),
                "median_roi": round(float(np.median(pnl)), 4),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline empirical entry model v2")
    parser.add_argument("--archive", default=DEFAULT_ARCHIVE)
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--alpha", type=float, default=2.0)
    parser.add_argument("--beta", type=float, default=2.0)
    parser.add_argument("--min-group", type=int, default=20)
    parser.add_argument("--bootstrap", type=int, default=2000)
    args = parser.parse_args()

    archive = Path(args.archive)
    samples, load_stats = load_tokens(archive)
    if not samples:
        print("[entry_model_v2] no resolved tokens found", flush=True)
        return 2

    print(f"[entry_model_v2] resolved tokens={len(samples)} load={load_stats}", flush=True)

    market = {s["token_id"]: s["price"] for s in samples}
    current = {s["token_id"]: s["p_est"] for s in samples}

    def band_key(sample: dict) -> str:
        return price_band(sample["price"])

    def band_roc_key(sample: dict) -> str:
        return f"{price_band(sample['price'])}|{roc_sign(sample['roc'])}"

    emp_band, band_meta = empirical_loo(samples, band_key, None, args.alpha, args.beta, args.min_group)
    emp_band_roc, roc_meta = empirical_loo(
        samples, band_roc_key, band_key, args.alpha, args.beta, args.min_group
    )

    models = {
        "market": market,
        "current_p_est": current,
        "emp_band": emp_band,
        "emp_band_roc": emp_band_roc,
    }
    comparison = {}
    for name, pred in models.items():
        comparison[name] = {
            "brier": round(brier(pred, samples), 6),
            "logloss": round(logloss(pred, samples), 6),
            "brier_diff_vs_market": bootstrap_diff(pred, market, samples, args.bootstrap),
            "mean_prediction": round(float(np.mean([pred[s["token_id"]] for s in samples])), 6),
            "hit_rate": round(float(np.mean([s["outcome"] for s in samples])), 6),
            "mean_price": round(float(np.mean([s["price"] for s in samples])), 6),
        }

    best_name = min((name for name in models if name != "market"), key=lambda n: comparison[n]["brier"])
    thresholds = [0.0, 0.02, 0.05]
    report = {
        "generated_at": now_utc(),
        "date": args.date,
        "archive": str(archive),
        "coverage": {
            "resolved_tokens": len(samples),
            "variants": dict(load_stats["variants"]),
            "eval_records_seen": load_stats["eval_records"],
            "categories": dict(Counter(s["category"] for s in samples)),
            "price_bands": dict(Counter(price_band(s["price"]) for s in samples)),
        },
        "settings": {
            "alpha": args.alpha,
            "beta": args.beta,
            "min_group": args.min_group,
            "bootstrap": args.bootstrap,
            "validation": "leave-one-token-out",
        },
        "comparison": comparison,
        "best_model": best_name,
        "calibration": calibration(models[best_name], samples),
        "emp_band_table": full_table(samples, band_key, args.alpha, args.beta),
        "emp_band_roc_table": full_table(samples, band_roc_key, args.alpha, args.beta),
        "band_meta": band_meta,
        "roc_meta": roc_meta,
        "rules": {
            name: rule_eval(models[name], samples, thresholds)
            for name in ("current_p_est", "emp_band", "emp_band_roc")
        },
    }

    out_dir = archive / "models"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"entry_model_v2_{args.date}.json"
    md_path = out_dir / f"entry_model_v2_{args.date}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"# Entry Model v2 · Empirical Probability · {args.date}",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- resolved tokens: **{len(samples)}**",
        f"- validation: **leave-one-token-out** (one EVAL per token)",
        f"- best non-market model: **{best_name}**",
        "",
        "## Model comparison (LOO)",
        "",
        "| model | Brier | LogLoss | Brier diff vs market [point, lo, hi] | mean pred | hit | mean price |",
        "|---|---:|---:|---|---:|---:|---:|",
    ]
    for name, row in comparison.items():
        diff = row["brier_diff_vs_market"]
        lines.append(
            f"| {name} | {row['brier']:.6f} | {row['logloss']:.6f} | "
            f"[{diff[0]:+.6f}, {diff[1]:+.6f}, {diff[2]:+.6f}] | "
            f"{row['mean_prediction']:.4f} | {row['hit_rate']:.4f} | {row['mean_price']:.4f} |"
        )

    lines += ["", "## Price-band empirical table", "", "| band | n | wins | p_emp |", "|---|---:|---:|---:|"]
    for row in report["emp_band_table"]:
        lines.append(f"| {row['group']} | {row['n']} | {row['wins']} | {row['p_emp']:.4f} |")

    lines += ["", f"## Calibration · {best_name}", "", "| bin | n | mean_pred | observed |", "|---|---:|---:|---:|"]
    for row in report["calibration"]:
        lines.append(f"| {row['range'][0]:.1f}-{row['range'][1]:.1f} | {row['n']} | {row['mean_prediction']:.4f} | {row['observed_frequency']:.4f} |")

    lines += ["", "## Rule evaluation (select if p - price >= threshold)", "", "| model | threshold | n | hit | mean price | realized edge | mean ROI | median ROI |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, rows in report["rules"].items():
        for row in rows:
            if not row.get("n"):
                lines.append(f"| {name} | {row['threshold']:.2f} | 0 | — | — | — | — | — |")
            else:
                lines.append(
                    f"| {name} | {row['threshold']:.2f} | {row['n']} | {row['hit_rate']:.4f} | "
                    f"{row['mean_price']:.4f} | {row['realized_edge']:+.4f} | "
                    f"{row['mean_roi']:+.4f} | {row['median_roi']:+.4f} |"
                )

    lines += [
        "",
        "## Caveats",
        "",
        "- One EVAL per resolved token; unresolved tokens are excluded.",
        "- Resolved sample is biased toward short-horizon sports/weather.",
        "- LOO is not a time split; a model that learned the current market regime can still fail forward.",
        "- A better Brier is necessary but not sufficient for live trading.",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"best_model": best_name, "comparison": comparison, "report_md": str(md_path), "report_json": str(json_path)}, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

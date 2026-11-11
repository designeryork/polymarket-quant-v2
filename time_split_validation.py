#!/usr/bin/env python3
"""Pre-registered time-split validation for the frozen entry model.

Read-only research script. It does not import or modify quant_engine.py.

Protocol (locked before running; see Docs/061):
    Snapshot   : all resolved unique tokens as of 2026-11-11
    Train      : earliest EVAL on 2026-11-06 or 2026-11-07
    Validation : 2026-11-08
    Test       : 2026-11-09 or 2026-11-10

Models:
    market          price baseline
    current_p_est   frozen engine p_est (no fitting)
    emp_band        P(win | price band), Beta(2,2), fit on train only
    emp_band_roc    P(win | band x ROC sign), fit on train, fallback band

Pass criterion:
    At least one non-market model has a test Brier difference vs market whose
    bootstrap 95% CI upper bound is < 0. A positive result is only a license
    for forward paper testing, never for live trading.

Usage:
    python time_split_validation.py --date 2026-11-11
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from entry_model_v2 import (
    brier,
    bootstrap_diff,
    calibration,
    load_tokens,
    logloss,
    price_band,
    roc_sign,
    rule_eval,
)
from resolve_research import DEFAULT_ARCHIVE

TRAIN_DAYS = ("2026-11-06", "2026-11-07")
VALID_DAYS = ("2026-11-08",)
TEST_DAYS = ("2026-11-09", "2026-11-10")
THRESHOLDS = (0.0, 0.02, 0.05)
ALPHA = 2.0
BETA = 2.0
MIN_GROUP = 20


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def day_of(sample: dict) -> str:
    return str(sample.get("ts_utc") or "")[:10]


def fit_empirical(
    train: list[dict],
    key_fn: Callable[[dict], Any],
    parent_fn: Optional[Callable[[dict], Any]] = None,
) -> tuple[Callable[[dict], float], dict]:
    """Fit a Beta-binomial table on train and return a predictor for unseen rows."""
    group_n: Counter = Counter()
    group_w: Counter = Counter()
    parent_n: Counter = Counter()
    parent_w: Counter = Counter()
    for sample in train:
        key = key_fn(sample)
        group_n[key] += 1
        group_w[key] += sample["outcome"]
        if parent_fn:
            parent = parent_fn(sample)
            parent_n[parent] += 1
            parent_w[parent] += sample["outcome"]
    total_n = len(train)
    total_w = sum(sample["outcome"] for sample in train)

    def predict(sample: dict) -> float:
        key = key_fn(sample)
        n = group_n[key]
        if n >= MIN_GROUP:
            return (group_w[key] + ALPHA) / (n + ALPHA + BETA)
        if parent_fn:
            parent = parent_fn(sample)
            pn = parent_n[parent]
            if pn >= MIN_GROUP:
                return (parent_w[parent] + ALPHA) / (pn + ALPHA + BETA)
        return (total_w + ALPHA) / (total_n + ALPHA + BETA)

    table = {
        "groups": {
            str(key): {
                "n": group_n[key],
                "wins": group_w[key],
                "p_emp": round((group_w[key] + ALPHA) / (group_n[key] + ALPHA + BETA), 6),
            }
            for key in sorted(group_n, key=str)
        },
        "fallback_global": round((total_w + ALPHA) / (total_n + ALPHA + BETA), 6),
    }
    return predict, table


def evaluate(
    name: str,
    pred: dict[str, float],
    samples: list[dict],
    bootstrap: int,
) -> dict:
    market = {sample["token_id"]: float(sample["price"]) for sample in samples}
    return {
        "model": name,
        "n": len(samples),
        "brier": round(brier(pred, samples), 6),
        "logloss": round(logloss(pred, samples), 6),
        "brier_diff_vs_market": bootstrap_diff(pred, market, samples, bootstrap=bootstrap),
        "mean_prediction": round(float(np.mean([pred[s["token_id"]] for s in samples])), 6),
        "hit_rate": round(float(np.mean([s["outcome"] for s in samples])), 6),
        "mean_price": round(float(np.mean([s["price"] for s in samples])), 6),
    }


def evaluate_split(
    name: str,
    split: list[dict],
    market_pred: dict[str, float],
    current_pred: dict[str, float],
    emp_band_pred: dict[str, float],
    emp_band_roc_pred: dict[str, float],
    bootstrap: int,
) -> dict:
    models = {
        "market": market_pred,
        "current_p_est": current_pred,
        "emp_band": emp_band_pred,
        "emp_band_roc": emp_band_roc_pred,
    }
    rows = [evaluate(model, pred, split, bootstrap) for model, pred in models.items()]
    return {
        "name": name,
        "n": len(split),
        "days": dict(Counter(day_of(s) for s in split)),
        "models": rows,
        "rules_current_p_est": rule_eval(current_pred, split, list(THRESHOLDS)),
        "rules_emp_band": rule_eval(emp_band_pred, split, list(THRESHOLDS)),
        "calibration_current_p_est": calibration(current_pred, split),
    }


def render(report: dict) -> str:
    lines = [
        f"# Time-Split Entry Validation · {report['date']}",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- snapshot hash: `{report['snapshot_hash']}`",
        f"- resolved unique tokens: **{report['snapshot_tokens']}**",
        "- Protocol: train `2026-11-06/07`, validation `2026-11-08`, test `2026-11-09/10`.",
        "- **Read-only. No engine change, no parameter tuning.**",
        "",
        "## Split sizes",
        "",
        "| split | n | days |",
        "|---|---:|---|",
    ]
    for split in report["splits"]:
        days = ", ".join(f"{k}:{v}" for k, v in sorted(split["days"].items()))
        lines.append(f"| {split['name']} | {split['n']} | {days} |")

    lines += ["", "## Brier (each split)", ""]
    for split in report["splits"]:
        lines += [
            f"### {split['name']} (n={split['n']})",
            "",
            "| model | Brier | LogLoss | Brier diff vs market [point, lo, hi] |",
            "|---|---:|---:|---|",
        ]
        for row in split["models"]:
            diff = row["brier_diff_vs_market"]
            lines.append(
                f"| {row['model']} | {row['brier']:.6f} | {row['logloss']:.6f} | "
                f"{diff[0]:+.6f} [{diff[1]:+.6f}, {diff[2]:+.6f}] |"
            )
        lines.append("")

    lines += ["## Test-set rules (current_p_est)", ""]
    test = next(s for s in report["splits"] if s["name"] == "test")
    lines += ["| threshold | n | hit | mean price | realized edge |", "|---:|---:|---:|---:|---:|"]
    for row in test["rules_current_p_est"]:
        if row.get("n"):
            lines.append(
                f"| {row['threshold']:.2f} | {row['n']} | {row['hit_rate']:.3f} | "
                f"{row['mean_price']:.3f} | {row['realized_edge']:+.4f} |"
            )
        else:
            lines.append(f"| {row['threshold']:.2f} | 0 | — | — | — |")

    lines += [
        "",
        "## Decision",
        "",
        report["decision"],
        "",
        "## Caveats",
        "",
        "- One sample per resolved token; unresolved tokens are excluded.",
        "- The test set is small; a negative result is enough to stop this model family, not to prove the market is efficient everywhere.",
        "- EVAL prices are not a full execution simulation: no queue, no partial fill, no fees beyond the recorded price.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-registered time-split entry validation")
    parser.add_argument("--archive", default=str(DEFAULT_ARCHIVE))
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--bootstrap", type=int, default=2000)
    args = parser.parse_args()

    archive = Path(args.archive)
    samples, load_stats = load_tokens(archive)
    if not samples:
        print("[time_split] no resolved tokens found", flush=True)
        return 2
    samples.sort(key=lambda s: str(s.get("ts_utc") or ""))

    snapshot = sorted((s["token_id"], int(s["outcome"])) for s in samples)
    snapshot_hash = hashlib.sha256(
        json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]

    train = [s for s in samples if day_of(s) in TRAIN_DAYS]
    valid = [s for s in samples if day_of(s) in VALID_DAYS]
    test = [s for s in samples if day_of(s) in TEST_DAYS]
    if not train or not test:
        print(f"[time_split] empty split: train={len(train)} val={len(valid)} test={len(test)}", flush=True)
        return 2

    band_predict, band_table = fit_empirical(train, lambda s: price_band(s["price"]))
    band_roc_predict, band_roc_table = fit_empirical(
        train,
        lambda s: (price_band(s["price"]), roc_sign(s["roc"])),
        lambda s: price_band(s["price"]),
    )

    def preds(split: list[dict]) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, float]]:
        market = {s["token_id"]: float(s["price"]) for s in split}
        current = {s["token_id"]: float(s["p_est"]) for s in split}
        band = {s["token_id"]: float(band_predict(s)) for s in split}
        band_roc = {s["token_id"]: float(band_roc_predict(s)) for s in split}
        return market, current, band, band_roc

    splits = []
    for name, part in (("train", train), ("validation", valid), ("test", test)):
        market, current, band, band_roc = preds(part)
        splits.append(
            evaluate_split(name, part, market, current, band, band_roc, args.bootstrap)
        )

    test_row = next(s for s in splits if s["name"] == "test")
    test_models = {row["model"]: row for row in test_row["models"]}
    passed = [
        name
        for name, row in test_models.items()
        if name != "market" and row["brier_diff_vs_market"][2] < 0
    ]
    if passed:
        decision = (
            f"PASS for {', '.join(passed)} on the test split: Brier CI upper < 0. "
            "Next step is forward paper testing only; do not go live."
        )
    else:
        decision = (
            "NO-GO: no candidate has a test Brier difference vs market with CI upper < 0. "
            "Close this entry-model family; do not tune on the same 1,028 tokens."
        )

    report = {
        "generated_at": now_utc(),
        "date": args.date,
        "snapshot_tokens": len(samples),
        "snapshot_hash": snapshot_hash,
        "load_stats": {
            "variants": dict(load_stats.get("variants") or {}),
            "eval_records": load_stats.get("eval_records"),
            "duplicates_skipped": load_stats.get("duplicates_skipped"),
        },
        "protocol": {
            "train_days": list(TRAIN_DAYS),
            "validation_days": list(VALID_DAYS),
            "test_days": list(TEST_DAYS),
            "alpha": ALPHA,
            "beta": BETA,
            "min_group": MIN_GROUP,
            "bootstrap": args.bootstrap,
        },
        "train_band_table": band_table,
        "train_band_roc_table": band_roc_table,
        "splits": splits,
        "decision": decision,
    }

    out_dir = archive / "models"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"time_split_{args.date}.json"
    md_path = out_dir / f"time_split_{args.date}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render(report), encoding="utf-8")
    print(json.dumps({
        "tokens": len(samples),
        "hash": snapshot_hash,
        "splits": {s["name"]: s["n"] for s in splits},
        "passed": passed,
        "decision": decision,
        "report_md": str(md_path),
        "report_json": str(json_path),
    }, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

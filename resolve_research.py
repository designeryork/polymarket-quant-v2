#!/usr/bin/env python3
"""Resolve every archived EVAL token to its final 1/0 outcome.

Phase 2.1 — measurement only. It does NOT touch quant_engine.py or the live
paper ledger; it reads the local research archive and writes a resolution
table next to it.

Inputs (default):
    D:\\2026_APPs by AI\\_research_archive\\v1|v2\\artifacts\\**\\strategy_research_samples.jsonl
    D:\\2026_APPs by AI\\_research_archive\\v1|v2\\releases\\**\\*.tar.gz

Outputs:
    <variant_root>\\resolutions\\resolutions.jsonl   one record per token
    <variant_root>\\resolutions\\summary.json        counters
    <variant_root>\\resolutions\\resolve.log         log

Usage:
    python resolve_research.py --variant v2 --limit 20
    python resolve_research.py --variant v2 --dry-run
    python resolve_research.py --input logs/strategy_research_samples.jsonl \
        --out-dir D:/tmp/res_test --limit 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com").rstrip("/")
GAMMA_HOST = "https://gamma-api.polymarket.com"
DEFAULT_ARCHIVE = r"D:\2026_APPs by AI\_research_archive"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class Logger:
    def __init__(self, path: Optional[Path]):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, message: str) -> None:
        line = f"[{now_utc()}] {message}"
        print(line, flush=True)
        if self.path:
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass


def build_session():
    try:
        import proxy_detector  # type: ignore

        return proxy_detector.build_session()
    except Exception:
        import requests

        session = requests.Session()
        session.headers.update({"User-Agent": "PolymarketResolve/1.0"})
        return session


def iter_text_sources(variant_root: Path) -> Iterator[tuple[str, str]]:
    """Yield (source_label, jsonl_text) from artifacts and release tarballs."""
    artifacts = variant_root / "artifacts"
    if artifacts.exists():
        for path in sorted(artifacts.rglob("strategy_research_samples.jsonl")):
            try:
                yield str(path), path.read_text(encoding="utf-8")
            except OSError as exc:
                yield str(path), ""
    releases = variant_root / "releases"
    if releases.exists():
        for path in sorted(releases.rglob("*.tar.gz")):
            try:
                with tarfile.open(path, "r:gz") as tar:
                    for member in tar.getmembers():
                        if member.isfile() and member.name.endswith("strategy_research_samples.jsonl"):
                            handle = tar.extractfile(member)
                            if handle:
                                yield f"{path}:{member.name}", handle.read().decode("utf-8", "replace")
            except (OSError, tarfile.TarError) as exc:
                yield str(path), ""


def collect_tokens(sources: Iterable[tuple[str, str]]) -> tuple[int, dict[str, dict]]:
    eval_count = 0
    tokens: dict[str, dict] = {}
    for source, text in sources:
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("record_type") != "EVAL":
                continue
            eval_count += 1
            token_id = str(record.get("token_id") or "")
            if not token_id:
                continue
            previous = tokens.get(token_id)
            if previous and str(previous.get("ts_utc") or "") > str(record.get("ts_utc") or ""):
                continue
            tokens[token_id] = record
    return eval_count, tokens


def load_resolved(path: Path) -> dict[str, dict]:
    resolved: dict[str, dict] = {}
    if not path.exists():
        return resolved
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("outcome") in (0, 1):
                resolved[str(record.get("token_id"))] = record
    except OSError:
        pass
    return resolved


def parse_clob(payload: dict, token_id: str) -> Optional[int]:
    if not payload.get("closed"):
        return None
    for token in payload.get("tokens") or []:
        if str(token.get("token_id")) != token_id:
            continue
        price = token.get("price")
        if price in (0, 1):
            return int(price)
        if token.get("winner") is True:
            return 1
        if token.get("winner") is False:
            return 0
    return None


def parse_gamma(rows: list, token_id: str, label: str) -> Optional[int]:
    for row in rows:
        if not row.get("closed"):
            continue
        prices = row.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except json.JSONDecodeError:
                prices = []
        token_ids = row.get("clobTokenIds")
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except json.JSONDecodeError:
                token_ids = []
        index = None
        for idx, value in enumerate(token_ids or []):
            if str(value) == token_id:
                index = idx
                break
        if index is None:
            outcomes = row.get("outcomes")
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except json.JSONDecodeError:
                    outcomes = []
            for idx, name in enumerate(outcomes or []):
                if str(name).strip().lower() == str(label).strip().lower():
                    index = idx
                    break
        if index is not None and index < len(prices or []):
            try:
                price = float(prices[index])
            except (TypeError, ValueError):
                continue
            if price >= 0.99:
                return 1
            if price <= 0.01:
                return 0
    return None


def query_resolution(session, record: dict) -> tuple[Optional[int], str]:
    token_id = str(record.get("token_id") or "")
    event_id = str(record.get("event_id") or "")
    label = str(record.get("outcome_label") or "")
    last_error = "no_event_id"

    if event_id:
        try:
            response = session.get(f"{CLOB_HOST}/markets/{event_id}", timeout=20)
            if response.status_code == 200:
                payload = response.json()
                if not payload.get("closed"):
                    # CLOB 明确未收盘：不必再问 Gamma。
                    return None, "not_closed"
                outcome = parse_clob(payload, token_id)
                if outcome is not None:
                    return outcome, "clob"
                last_error = "clob_closed_no_price"
            elif response.status_code in (403, 404):
                last_error = f"clob_http_{response.status_code}"
            else:
                return None, f"clob_http_{response.status_code}"
        except Exception as exc:
            last_error = f"clob_{type(exc).__name__}"

    # Gamma fallback by token id.
    try:
        response = session.get(
            f"{GAMMA_HOST}/markets",
            params={"clob_token_ids": token_id, "closed": "true"},
            timeout=20,
        )
        if response.status_code == 200:
            rows = response.json()
            if isinstance(rows, dict):
                rows = rows.get("data") or []
            outcome = parse_gamma(rows if isinstance(rows, list) else [], token_id, label)
            if outcome is not None:
                return outcome, "gamma"
            return None, "gamma_not_resolved"
        return None, f"gamma_http_{response.status_code}"
    except Exception as exc:
        return None, f"{last_error};gamma_{type(exc).__name__}"


def run_variant(
    variant: str,
    variant_root: Path,
    session,
    limit: int,
    sleep_seconds: float,
    dry_run: bool,
    log: Logger,
) -> dict:
    sources = list(iter_text_sources(variant_root))
    eval_count, tokens = collect_tokens(sources)
    out_dir = variant_root / "resolutions"
    out_path = out_dir / "resolutions.jsonl"
    resolved = load_resolved(out_path)

    pending_all = [tok for tok in tokens if tok not in resolved]
    # 最新评估优先：短周期赛事/天气通常先结算，能让校准样本更快增长。
    pending_all.sort(key=lambda tok: str(tokens[tok].get("ts_utc") or ""), reverse=True)
    rotation_path = out_dir / f"rotation_{variant}.json"
    try:
        start = int(json.loads(rotation_path.read_text(encoding="utf-8")).get("next_offset", 0))
    except Exception:
        start = 0
    if limit > 0 and pending_all:
        start = start % len(pending_all)
        ordered = pending_all[start:] + pending_all[:start]
        pending = ordered[:limit]
        next_offset = (start + len(pending)) % len(pending_all)
    else:
        pending = pending_all
        next_offset = 0

    log(
        f"{variant}: sources={len(sources)} eval_records={eval_count} "
        f"unique_tokens={len(tokens)} already_resolved={len(resolved)} pending={len(pending)}"
    )

    new_records = []
    unresolved = 0
    errors = 0
    by_source: dict[str, int] = {}

    for token_id in pending:
        record = tokens[token_id]
        question = str(record.get("question") or "")[:80]
        if dry_run:
            log(f"  would query {token_id[:12]}… {question}")
            continue
        outcome, source = query_resolution(session, record)
        if outcome is None:
            unresolved += 1
            log(f"  unresolved {token_id[:12]}… ({source}) {question}")
        else:
            new_records.append(
                {
                    "record_type": "RESOLUTION",
                    "token_id": token_id,
                    "event_id": record.get("event_id"),
                    "outcome": outcome,
                    "source": source,
                    "resolved_at": now_utc(),
                    "variant": record.get("variant") or variant,
                    "strategy_version": record.get("strategy_version"),
                    "question": record.get("question"),
                    "outcome_label": record.get("outcome_label"),
                    "p_est": record.get("p_est"),
                    "signal_price": record.get("signal_price"),
                    "gate_result": record.get("gate_result"),
                    "metrics": record.get("metrics"),
                }
            )
            by_source[source] = by_source.get(source, 0) + 1
            log(f"  resolved {token_id[:12]}… = {outcome} ({source}) {question}")
        time.sleep(max(0.0, sleep_seconds))

    summary = {
        "generated_at": now_utc(),
        "variant": variant,
        "variant_root": str(variant_root),
        "source_files": len(sources),
        "eval_records": eval_count,
        "unique_tokens": len(tokens),
        "already_resolved": len(resolved),
        "queried": len(pending),
        "newly_resolved": len(new_records),
        "unresolved": unresolved,
        "errors": errors,
        "by_source": by_source,
        "rotation_next_offset": next_offset,
        "pending_unresolved": len(pending_all),
        "dry_run": dry_run,
    }

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        if new_records:
            with open(out_path, "a", encoding="utf-8") as handle:
                for record in new_records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        (out_dir / f"summary_{variant}.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / f"rotation_{variant}.json").write_text(
            json.dumps(
                {
                    "next_offset": next_offset,
                    "pending_unresolved": len(pending_all),
                    "updated_at": now_utc(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        log(f"{variant}: wrote {len(new_records)} new resolutions -> {out_path}")
    log(f"{variant}: summary {json.dumps(summary, ensure_ascii=False)}")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve archived EVAL tokens to 1/0")
    parser.add_argument("--archive", default=DEFAULT_ARCHIVE)
    parser.add_argument("--variant", choices=["v1", "v2", "all"], default="v2")
    parser.add_argument("--input", default="", help="single JSONL for testing; overrides archive discovery")
    parser.add_argument("--out-dir", default="", help="output dir when using --input")
    parser.add_argument("--limit", type=int, default=200, help="max new tokens per variant (0 = all)")
    parser.add_argument("--sleep", type=float, default=0.2, help="seconds between requests")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    archive = Path(args.archive)
    session = build_session()

    if args.input:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"input not found: {input_path}", file=sys.stderr)
            return 2
        variant = args.variant if args.variant in ("v1", "v2") else "v2"
        out_dir = Path(args.out_dir) if args.out_dir else archive / variant / "resolutions"
        out_dir.mkdir(parents=True, exist_ok=True)
        log = Logger(out_dir / "resolve.log")
        source_text = input_path.read_text(encoding="utf-8")
        eval_count, tokens = collect_tokens([(str(input_path), source_text)])
        resolved = load_resolved(out_dir / "resolutions.jsonl")
        pending = [tok for tok in tokens if tok not in resolved]
        if args.limit > 0:
            pending = pending[: args.limit]
        log(f"input={input_path} eval_records={eval_count} unique={len(tokens)} pending={len(pending)}")
        new_records = []
        for token_id in pending:
            record = tokens[token_id]
            if args.dry_run:
                log(f"  would query {token_id[:12]}… {record.get('question', '')[:70]}")
                continue
            outcome, source = query_resolution(session, record)
            if outcome is None:
                log(f"  unresolved {token_id[:12]}… ({source})")
            else:
                new_records.append(
                    {
                        "record_type": "RESOLUTION",
                        "token_id": token_id,
                        "event_id": record.get("event_id"),
                        "outcome": outcome,
                        "source": source,
                        "resolved_at": now_utc(),
                        "variant": record.get("variant") or variant,
                        "strategy_version": record.get("strategy_version"),
                        "question": record.get("question"),
                        "outcome_label": record.get("outcome_label"),
                        "p_est": record.get("p_est"),
                        "signal_price": record.get("signal_price"),
                        "gate_result": record.get("gate_result"),
                        "metrics": record.get("metrics"),
                    }
                )
                log(f"  resolved {token_id[:12]}… = {outcome} ({source})")
            time.sleep(max(0.0, args.sleep))
        if not args.dry_run and new_records:
            with open(out_dir / "resolutions.jsonl", "a", encoding="utf-8") as handle:
                for record in new_records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        log(f"done new={len(new_records)}")
        return 0

    variants = ["v1", "v2"] if args.variant == "all" else [args.variant]
    summaries = []
    for variant in variants:
        root = archive / variant
        if not root.exists():
            print(f"[resolve] skip {variant}: {root} not found", flush=True)
            continue
        out_dir = root / "resolutions"
        log = Logger(out_dir / "resolve.log")
        summaries.append(
            run_variant(variant, root, session, args.limit, args.sleep, args.dry_run, log)
        )
    print(json.dumps(summaries, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

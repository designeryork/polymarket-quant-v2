#!/usr/bin/env python3
"""Forward-only order-flow / depth shadow collector (read-only research).

No keys, no orders, no engine changes. It records a snapshot of:

  * Gamma market metadata (question, end date, category, fees);
  * CLOB YES/NO best bid/ask and top-of-book depth;
  * public trades in the last 5/15/60 minutes, normalised to YES shares;
  * the derived 15m taker-flow imbalance used by the pre-registered test.

The output is an append-only JSONL file. It is intentionally collected
**forward only** (no backfill); the evaluation script joins tokens to their
final resolution later.

Usage:
    python flow_shadow.py --limit 60 --out-dir state/flow_shadow
    python flow_shadow.py --limit 3 --min-hours 0 --max-hours 100000
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import proxy_detector

from daily_metrics import market_category
from parity_scan import (
    fee_per_share,
    fetch_universe,
    hours_left,
    json_list,
    parse_float,
)

CLOB_HOST = "https://clob.polymarket.com"
DATA_HOST = "https://data-api.polymarket.com"
WINDOWS = (5, 15, 60)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_book(session, token_id: str, timeout: int = 20) -> Optional[dict]:
    """Return best bid/ask, top size, and depth within 2 cents."""
    try:
        response = session.get(
            f"{CLOB_HOST}/book",
            params={"token_id": token_id},
            timeout=timeout,
        )
        if response.status_code != 200:
            return None
        payload = response.json()
        bids = payload.get("bids") or []
        asks = payload.get("asks") or []
        if not bids or not asks:
            return None
        bid_rows = [
            (float(item.get("price") or 0), float(item.get("size") or 0))
            for item in bids
            if float(item.get("price") or 0) > 0
        ]
        ask_rows = [
            (float(item.get("price") or 0), float(item.get("size") or 0))
            for item in asks
            if float(item.get("price") or 0) > 0
        ]
        if not bid_rows or not ask_rows:
            return None
        best_bid = max(price for price, _ in bid_rows)
        best_ask = min(price for price, _ in ask_rows)
        bid_top = sum(size for price, size in bid_rows if abs(price - best_bid) < 1e-9)
        ask_top = sum(size for price, size in ask_rows if abs(price - best_ask) < 1e-9)
        bid_depth = sum(
            size for price, size in bid_rows if price >= best_bid - 0.02
        )
        ask_depth = sum(
            size for price, size in ask_rows if price <= best_ask + 0.02
        )
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_top_usd": best_bid * bid_top,
            "ask_top_usd": best_ask * ask_top,
            "bid_depth_usd": best_bid * bid_depth,
            "ask_depth_usd": best_ask * ask_depth,
        }
    except Exception:
        return None


def fetch_trades(session, condition_id: str, limit: int = 200) -> tuple[list[dict], Optional[str]]:
    """Fetch recent public trades for a condition, with small retry budget."""
    last_error = None
    for attempt in range(3):
        try:
            response = session.get(
                f"{DATA_HOST}/v2/trades",
                params={"condition": condition_id, "limit": max(1, min(limit, 500))},
                timeout=20,
            )
            if response.status_code == 429:
                last_error = "http_429"
                time.sleep(2 + attempt * 2)
                continue
            if response.status_code >= 500:
                last_error = f"http_{response.status_code}"
                time.sleep(1 + attempt)
                continue
            if response.status_code != 200:
                return [], f"http_{response.status_code}"
            payload = response.json()
            rows = payload.get("data") if isinstance(payload, dict) else payload
            return (rows if isinstance(rows, list) else []), None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(1 + attempt)
    return [], last_error


def flow_window(
    rows: list[dict],
    yes_token: str,
    no_token: str,
    now_ts: float,
    minutes: int,
) -> dict:
    """Signed YES-equivalent taker flow over a trailing window."""
    start = now_ts - minutes * 60
    net_shares = 0.0
    total_shares = 0.0
    net_usd = 0.0
    count = 0
    buy_yes = sell_yes = buy_no = sell_no = 0.0
    for row in rows:
        try:
            ts = float(row.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if ts < start or ts > now_ts + 60:
            continue
        side = str(row.get("side") or "").upper()
        token = str(row.get("token_id") or "")
        outcome = str(row.get("outcome") or "").strip().lower()
        is_yes = token == yes_token or (not token and outcome == "yes")
        is_no = token == no_token or (not token and outcome == "no")
        if not is_yes and not is_no:
            continue
        try:
            size = float(row.get("size") or 0)
            price = float(row.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        if is_yes:
            sign = 1.0 if side == "BUY" else -1.0
            if side == "BUY":
                buy_yes += size
            else:
                sell_yes += size
        else:
            # Buying NO is economically selling YES; selling NO is buying YES.
            sign = -1.0 if side == "BUY" else 1.0
            if side == "BUY":
                buy_no += size
            else:
                sell_no += size
        net_shares += sign * size
        net_usd += sign * size * price
        total_shares += size
        count += 1
    return {
        "minutes": minutes,
        "trade_count": count,
        "net_yes_shares": round(net_shares, 4),
        "total_shares": round(total_shares, 4),
        "flow_ratio": round(net_shares / total_shares, 6) if total_shares > 0 else None,
        "net_yes_usd": round(net_usd, 4),
        "buy_yes_shares": round(buy_yes, 4),
        "sell_yes_shares": round(sell_yes, 4),
        "buy_no_shares": round(buy_no, 4),
        "sell_no_shares": round(sell_no, 4),
    }


def select_markets(
    markets: list[dict],
    limit: int,
    min_hours: float,
    max_hours: float,
) -> list[dict]:
    candidates = []
    for market in markets:
        if not market.get("active") or market.get("closed"):
            continue
        tokens = json_list(market.get("clobTokenIds"))
        outcomes = [str(x).strip().lower() for x in json_list(market.get("outcomes"))]
        if len(tokens) != 2 or set(outcomes) != {"yes", "no"}:
            continue
        remaining = hours_left(market)
        if remaining < min_hours or remaining > max_hours:
            continue
        volume = parse_float(market.get("volume24hr"))
        if volume is None:
            volume = parse_float(market.get("volume")) or 0.0
        yes_index = outcomes.index("yes")
        no_index = 1 - yes_index
        candidates.append(
            {
                "market": market,
                "yes_token": str(tokens[yes_index]),
                "no_token": str(tokens[no_index]),
                "volume": volume,
                "hours_left": remaining,
            }
        )
    candidates.sort(key=lambda row: row["volume"], reverse=True)
    return candidates[: max(0, limit)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Forward-only flow/depth shadow collector")
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--min-hours", type=float, default=2.0)
    parser.add_argument("--max-hours", type=float, default=168.0)
    parser.add_argument("--trades-limit", type=int, default=200)
    parser.add_argument("--sleep", type=float, default=0.15)
    parser.add_argument("--out-dir", default="state/flow_shadow")
    parser.add_argument("--run-id", default=str(int(time.time())))
    args = parser.parse_args()

    session = proxy_detector.build_session()
    session.headers.update({"User-Agent": "PolymarketFlowShadow/1.0"})
    started = time.time()
    universe, discovered = fetch_universe(session, args.sleep)
    if not universe:
        print("[flow_shadow] universe empty; abort", flush=True)
        return 2
    selected = select_markets(universe, args.limit, args.min_hours, args.max_hours)
    print(
        f"[flow_shadow] universe={len(universe)} discovered={discovered} selected={len(selected)}",
        flush=True,
    )

    rows: list[dict] = []
    errors: list[dict] = []
    for index, item in enumerate(selected, 1):
        market = item["market"]
        condition_id = str(market.get("conditionId") or "")
        yes_token = item["yes_token"]
        no_token = item["no_token"]
        if not condition_id or not yes_token or not no_token:
            errors.append({"condition_id": condition_id, "error": "missing_ids"})
            continue

        yes_book = fetch_book(session, yes_token)
        time.sleep(max(0.0, args.sleep))
        no_book = fetch_book(session, no_token)
        time.sleep(max(0.0, args.sleep))
        if not yes_book or not no_book:
            errors.append({"condition_id": condition_id, "error": "book_unavailable"})
            continue

        trades, trades_error = fetch_trades(session, condition_id, args.trades_limit)
        time.sleep(max(0.0, args.sleep))
        now_ts = time.time()
        flows = {
            f"{minutes}m": flow_window(trades, yes_token, no_token, now_ts, minutes)
            for minutes in WINDOWS
        }
        yes_ask = yes_book["best_ask"]
        no_ask = no_book["best_ask"]
        row = {
            "record_type": "FLOW_SHADOW",
            "run_id": args.run_id,
            "observed_at": now_utc(),
            "ts_epoch": round(now_ts, 3),
            "condition_id": condition_id,
            "question": str(market.get("question") or ""),
            "category": market_category(str(market.get("question") or "")),
            "end_date": str(market.get("endDate") or ""),
            "hours_left": round(item["hours_left"], 4),
            "volume24hr": item["volume"],
            "yes_token_id": yes_token,
            "no_token_id": no_token,
            "yes_bid": yes_book["best_bid"],
            "yes_ask": yes_ask,
            "no_bid": no_book["best_bid"],
            "no_ask": no_ask,
            "yes_spread": round(yes_ask - yes_book["best_bid"], 6),
            "no_spread": round(no_ask - no_book["best_bid"], 6),
            "yes_bid_top_usd": round(yes_book["bid_top_usd"], 2),
            "yes_ask_top_usd": round(yes_book["ask_top_usd"], 2),
            "yes_bid_depth_usd": round(yes_book["bid_depth_usd"], 2),
            "yes_ask_depth_usd": round(yes_book["ask_depth_usd"], 2),
            "no_bid_top_usd": round(no_book["bid_top_usd"], 2),
            "no_ask_top_usd": round(no_book["ask_top_usd"], 2),
            "no_bid_depth_usd": round(no_book["bid_depth_usd"], 2),
            "no_ask_depth_usd": round(no_book["ask_depth_usd"], 2),
            "fees_enabled": bool(market.get("feesEnabled")),
            "fee_schedule": market.get("feeSchedule"),
            "fee_yes_ask": round(fee_per_share(market, yes_ask), 8),
            "fee_no_ask": round(fee_per_share(market, no_ask), 8),
            "flow": flows,
            "trades_error": trades_error,
        }
        rows.append(row)
        if index % 10 == 0:
            print(f"[flow_shadow] {index}/{len(selected)} rows={len(rows)}", flush=True)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    jsonl_path = out_dir / f"flow_shadow_{stamp}_{args.run_id}.jsonl"
    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    meta = {
        "generated_at": now_utc(),
        "run_id": args.run_id,
        "elapsed_seconds": round(time.time() - started, 2),
        "universe_markets": len(universe),
        "universe_discovered_at": discovered,
        "selected_markets": len(selected),
        "rows": len(rows),
        "errors": errors[:20],
        "settings": {
            "limit": args.limit,
            "min_hours": args.min_hours,
            "max_hours": args.max_hours,
            "trades_limit": args.trades_limit,
            "windows": list(WINDOWS),
        },
        "jsonl": str(jsonl_path),
    }
    meta_path = out_dir / f"flow_shadow_{stamp}_{args.run_id}.meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "rows": len(rows),
                "errors": len(errors),
                "jsonl": str(jsonl_path),
                "meta": str(meta_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())

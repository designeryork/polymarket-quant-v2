#!/usr/bin/env python3
"""Read-only parity / event-basket scanner (article 2026-11-06).

No keys, no orders, no engine changes.

Modes
-----
0. `--refresh-universe`: fetch a fresh active-market universe from Gamma
   instead of reading a local snapshot. Used by the scheduled Pages scan.
1. Offline (default): scan the local Gamma snapshot for **complete negRisk
   event baskets** (buy all YES legs; if sum ask + fees < 1, edge).
2. `--event-details`: fetch the official leg count for each negRisk event
   from Gamma. Without it, raw groups are never treated as candidates.
3. `--books`: also fetch CLOB order books for a limited set of markets and
   evaluate same-market parity (YES ask + NO ask + fees < 1).

Usage
-----
    python parity_scan.py
    python parity_scan.py --min-edge 0.015 --max-edge 0.10 --min-hours 2
    python parity_scan.py --event-details --event-detail-limit 120
    python parity_scan.py --books --book-limit 40
    python parity_scan.py --refresh-universe --event-details --books
    python parity_scan.py --snapshot ../032_polymarket/state/market_universe.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

BASE = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT = BASE / "state" / "market_universe.json"
DEFAULT_OUT = Path(r"D:\2026_APPs by AI\_research_archive\parity")
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def hours_left(market: dict) -> float:
    end = parse_dt(market.get("endDate") or market.get("endDateIso"))
    if not end:
        return 999.0
    return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 3600.0)


def json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def fee_per_share(market: dict, price: float) -> float:
    if not market.get("feesEnabled"):
        return 0.0
    schedule = market.get("feeSchedule") or {}
    try:
        rate = float(schedule.get("rate") or 0.0)
    except (TypeError, ValueError):
        rate = 0.0
    try:
        exponent = float(schedule.get("exponent") or 1.0)
    except (TypeError, ValueError):
        exponent = 1.0
    if rate <= 0 or exponent <= 0:
        return 0.0
    return rate * (price * (1.0 - price)) ** exponent


def load_snapshot(path: Path) -> tuple[list[dict], str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        markets = data.get("markets") or data.get("data") or []
        discovered = str(data.get("discovered_at_utc") or "")
    else:
        markets = data
        discovered = ""
    return markets, discovered


def fetch_universe(session, sleep_seconds: float = 0.15) -> tuple[list[dict], str]:
    """Fetch the current active-market universe from Gamma.

    Mirrors the engine's pagination guard: Gamma caps a page at 100, returns
    422 once the offset goes too deep, and ``active=true`` still includes
    expired markets. We stop on a short page or the 422 cap and drop anything
    whose end date has passed.
    """
    import time

    params = {
        "active": "true",
        "closed": "false",
        "limit": 100,
        "order": "volume24hr",
        "ascending": "false",
    }
    discovered: list[dict] = []
    offset = 0
    while offset < 10000:
        try:
            response = session.get(
                f"{GAMMA_HOST}/markets",
                params={**params, "offset": offset},
                timeout=20,
            )
        except Exception as exc:
            if discovered:
                print(f"[parity_scan] universe page offset={offset} failed: {exc}", flush=True)
                break
            raise
        if response.status_code == 422 and offset >= 2000 and discovered:
            print(f"[parity_scan] universe reached Gamma offset cap at {offset}", flush=True)
            break
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list) or not page:
            break
        alive = [market for market in page if hours_left(market) > 0]
        discovered.extend(alive)
        if len(page) < 100:
            break
        offset += 100
        time.sleep(max(0.0, sleep_seconds))

    unique: dict[str, dict] = {}
    for market in discovered:
        key = str(market.get("conditionId") or market.get("id") or market.get("question") or "")
        if key:
            unique[key] = market
    ordered = sorted(unique.values(), key=lambda row: str(row.get("conditionId") or row.get("id") or ""))
    return ordered, now_utc()


def event_groups(markets: list[dict]) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for market in markets:
        events = market.get("events") or []
        if not events:
            continue
        event = events[0]
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        group = groups.setdefault(event_id, {"meta": event, "markets": []})
        group["markets"].append(market)
    return groups


def quote(market: dict) -> dict:
    ask = parse_float(market.get("bestAsk"))
    bid = parse_float(market.get("bestBid"))
    return {
        "ask": ask,
        "bid": bid,
        "ask_ok": ask is not None and 0 < ask < 1,
        "bid_ok": bid is not None and 0 < bid < 1,
    }


def scan_baskets(
    markets: list[dict],
    min_edge: float,
    max_edge: float,
    min_hours: float,
    official_counts: Optional[dict[str, int]] = None,
) -> dict:
    groups = event_groups(markets)
    rejects: Counter = Counter()
    candidates: list[dict] = []
    short_screen: list[dict] = []
    suspicious: list[dict] = []
    examples: list[dict] = []
    eligible_events = 0
    verified_events = 0
    multi_leg_events = 0
    official_counts = official_counts or {}

    for event_id, group in groups.items():
        event_markets = group["markets"]
        if len(event_markets) < 2:
            continue
        multi_leg_events += 1
        meta = group["meta"]

        if not meta.get("enableNegRisk"):
            rejects["not_negrisk"] += 1
            continue
        if meta.get("closed"):
            rejects["event_closed"] += 1
            continue

        legs = []
        bad = None
        for market in event_markets:
            if not market.get("active") or market.get("closed"):
                bad = "leg_closed_or_inactive"
                break
            q = quote(market)
            if not q["ask_ok"]:
                bad = "missing_quote"
                break
            if hours_left(market) < min_hours:
                bad = "horizon"
                break
            legs.append((market, q))
        if bad:
            rejects[bad] += 1
            continue
        eligible_events += 1

        n = len(legs)
        cost = sum(q["ask"] for _, q in legs)
        fees = sum(fee_per_share(m, q["ask"]) for m, q in legs)
        edge = 1.0 - cost - fees
        bid_sum = sum(q["bid"] for _, q in legs if q["bid_ok"])
        short_screen_edge = bid_sum - (n - 1) - fees
        title = str(meta.get("title") or "")[:80]

        official_legs = official_counts.get(event_id)
        if official_legs is None:
            cumulative = meta.get("cumulativeMarkets")
            if isinstance(cumulative, int) and cumulative > 0:
                official_legs = cumulative

        record = {
            "type": "basket",
            "event_id": event_id,
            "title": title,
            "legs": n,
            "official_legs": official_legs,
            "cost": round(cost, 6),
            "fees": round(fees, 6),
            "edge": round(edge, 6),
            "bid_sum": round(bid_sum, 6),
            "short_screen_edge": round(short_screen_edge, 6),
            "completeness": "official_legs" if official_legs is not None else "unverified",
            "actionable": False,
            "needs_depth": True,
            "note": "verified legs; verify CLOB depth and atomic execution before acting",
        }

        # Article brake 1: official leg count must match exactly.
        if official_legs is None:
            rejects["group_unverified"] += 1
            if edge >= min_edge:
                suspicious.append({**record, "reason": "group_unverified"})
            continue
        if official_legs != n:
            rejects["group_incomplete"] += 1
            suspicious.append({**record, "reason": "group_incomplete"})
            continue

        verified_events += 1
        if edge > max_edge:
            rejects["borderline_edge"] += 1
            if len(examples) < 8:
                examples.append({**record, "reason": "borderline_edge"})
        elif edge >= min_edge:
            candidates.append(record)
        else:
            rejects["below_threshold"] += 1

        if short_screen_edge >= min_edge:
            short_screen.append({**record, "note": "sell-all-YES screen; buying NO needs NO asks/depth"})

    candidates.sort(key=lambda row: row["edge"], reverse=True)
    short_screen.sort(key=lambda row: row["short_screen_edge"], reverse=True)
    suspicious.sort(key=lambda row: row["edge"], reverse=True)
    return {
        "multi_leg_events": multi_leg_events,
        "eligible_events": eligible_events,
        "verified_events": verified_events,
        "candidates": candidates,
        "basket_short_screen": short_screen,
        "suspicious": suspicious[:20],
        "rejects": dict(rejects),
        "examples": examples,
    }


def fetch_book(session, token_id: str, timeout: int = 20) -> Optional[dict]:
    try:
        response = session.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=timeout)
        if response.status_code != 200:
            return None
        payload = response.json()
        bids = payload.get("bids") or []
        asks = payload.get("asks") or []
        best_bid = max((parse_float(item.get("price")) or 0.0) for item in bids) if bids else None
        best_ask = min((parse_float(item.get("price")) or 1.0) for item in asks) if asks else None
        ask_size = 0.0
        if asks and best_ask is not None:
            ask_size = sum(
                parse_float(item.get("size")) or 0.0
                for item in asks
                if abs((parse_float(item.get("price")) or 0.0) - best_ask) < 1e-9
            )
        return {"best_bid": best_bid, "best_ask": best_ask, "ask_size": ask_size}
    except Exception:
        return None


def scan_parity(
    markets: list[dict],
    session,
    book_limit: int,
    min_edge: float,
    max_edge: float,
    min_hours: float,
    min_depth_usd: float,
    size_usd: float,
    sleep_seconds: float,
) -> dict:
    import time

    candidates = []
    rejects: Counter = Counter()
    checked = 0
    for market in markets:
        if checked >= book_limit:
            break
        if not market.get("active") or market.get("closed"):
            continue
        tokens = [str(x) for x in json_list(market.get("clobTokenIds"))]
        if len(tokens) < 2:
            rejects["non_binary"] += 1
            continue
        if hours_left(market) < min_hours:
            rejects["horizon"] += 1
            continue
        yes_book = fetch_book(session, tokens[0])
        no_book = fetch_book(session, tokens[1])
        checked += 1
        time.sleep(max(0.0, sleep_seconds))
        if not yes_book or not no_book:
            rejects["book_unavailable"] += 1
            continue
        yes_ask = yes_book.get("best_ask")
        no_ask = no_book.get("best_ask")
        if not yes_ask or not no_ask:
            rejects["missing_quote"] += 1
            continue
        cost = yes_ask + no_ask
        fees = fee_per_share(market, yes_ask) + fee_per_share(market, no_ask)
        edge = 1.0 - cost - fees
        required_depth = max(min_depth_usd, size_usd)
        depth_ok = (
            yes_ask * yes_book.get("ask_size", 0.0) >= required_depth
            and no_ask * no_book.get("ask_size", 0.0) >= required_depth
        )
        record = {
            "type": "parity",
            "condition_id": market.get("conditionId"),
            "question": str(market.get("question") or "")[:80],
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "cost": round(cost, 6),
            "fees": round(fees, 6),
            "edge": round(edge, 6),
            "yes_depth_usd": round(yes_ask * yes_book.get("ask_size", 0.0), 2),
            "no_depth_usd": round(no_ask * no_book.get("ask_size", 0.0), 2),
            "actionable": False,
            "note": "verify atomic two-leg execution and fees before acting",
        }
        if edge > max_edge:
            rejects["borderline_edge"] += 1
        elif edge >= min_edge:
            if depth_ok:
                record["actionable"] = True
                candidates.append(record)
            else:
                rejects["thin_liquidity"] += 1
        else:
            rejects["below_threshold"] += 1
    candidates.sort(key=lambda row: row["edge"], reverse=True)
    return {
        "checked": checked,
        "candidates": candidates,
        "rejects": dict(rejects),
    }


def write_report(report: dict, out_dir: Path, date_label: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"parity_{date_label}.md"
    json_path = out_dir / f"parity_{date_label}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    coverage = report["coverage"]
    lines = [
        f"# Parity / Event Basket Scan · {date_label}",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- snapshot: `{report['snapshot']}`",
        f"- snapshot time: `{report.get('snapshot_at') or 'unknown'}`",
        "- **Read-only. No keys, no orders, no engine changes.**",
        "",
        "## Coverage",
        "",
        "| metric | value |",
        "|---|---:|",
    ]
    for key, value in coverage.items():
        lines.append(f"| {key} | {value} |")

    lines += ["", "## Basket candidates (verified)", ""]
    if report["basket"]["candidates"]:
        lines += ["| event | legs | cost | fees | edge | note |", "|---|---:|---:|---:|---:|---|"]
        for row in report["basket"]["candidates"][:20]:
            lines.append(
                f"| {row['title']} | {row['legs']} | {row['cost']:.4f} | {row['fees']:.4f} | "
                f"{row['edge']:+.4f} | {row['note']} |"
            )
    else:
        lines.append("No verified complete basket below the threshold.")

    lines += ["", "## Suspicious raw groups (unverified / incomplete)", ""]
    if report["basket"]["suspicious"]:
        lines += ["| event | legs | official | cost | edge | reason |", "|---|---:|---:|---:|---:|---|"]
        for row in report["basket"]["suspicious"][:12]:
            lines.append(
                f"| {row['title']} | {row['legs']} | {row.get('official_legs') or '—'} | "
                f"{row['cost']:.4f} | {row['edge']:+.4f} | {row['reason']} |"
            )
    else:
        lines.append("None.")

    lines += ["", "## Basket rejects", "", "| reason | count |", "|---|---:|"]
    for reason, count in sorted(report["basket"]["rejects"].items(), key=lambda x: -x[1]):
        lines.append(f"| {reason} | {count} |")

    lines += ["", "## Same-market parity", ""]
    parity = report["parity"]
    if parity.get("skipped"):
        lines.append(f"- skipped: {parity['skipped']}")
    else:
        lines += ["| market | yes_ask | no_ask | edge | depth yes/no |", "|---|---:|---:|---:|---|"]
        if parity["candidates"]:
            for row in parity["candidates"][:20]:
                lines.append(
                    f"| {row['question']} | {row['yes_ask']:.4f} | {row['no_ask']:.4f} | "
                    f"{row['edge']:+.4f} | {row['yes_depth_usd']:.0f}/{row['no_depth_usd']:.0f} |"
                )
        else:
            lines.append("No parity candidate above the threshold.")
        lines += ["", "| reason | count |", "|---|---:|"]
        for reason, count in sorted(parity.get("rejects", {}).items(), key=lambda x: -x[1]):
            lines.append(f"| {reason} | {count} |")

    lines += ["", "## Borderline examples (edge > max)", ""]
    if report["basket"]["examples"]:
        lines += ["| event | legs | cost | edge |", "|---|---:|---:|---:|"]
        for row in report["basket"]["examples"][:8]:
            lines.append(f"| {row['title']} | {row['legs']} | {row['cost']:.4f} | {row['edge']:+.4f} |")
    else:
        lines.append("None.")

    lines += [
        "",
        "## How to read 0",
        "",
        "- 0 candidates is a **normal reading**, not a failure.",
        "- Look at the reject reasons first: `not_negrisk` / `group_unverified` / `missing_quote` / `horizon` / `below_threshold` / `borderline_edge`.",
        "- If `not_negrisk` dominates: the market universe is not negRisk; do not treat fact-exclusive groups as risk-free.",
        "- If `group_unverified` dominates: official leg count is missing; do not lower the threshold; run `--event-details` or fix leg metadata first.",
        "- If `group_incomplete` appears: the scanned legs do not match the official event; this is the article's 28-vs-4 trap; reject it.",
        "- If `verified_events` is 0: you are in offline mode; raw groups are **not** candidates. Run `--event-details` (needs proxy) to verify official leg counts.",
        "- If `below_threshold` dominates: fees/spread consume the gap; this is the honest answer.",
        "- If `borderline_edge` appears: high edge is more likely a data error; inspect the event legs before believing it.",
        "",
        "## Caveats",
        "",
        "- Offline basket scan uses snapshot `bestAsk`; no depth, no atomic execution check.",
        "- `negRiskAugmented` is a completeness proxy; live mode must verify official leg count.",
        "- Parity requires two real CLOB books; offline mode skips it.",
        "- Paper/snapshot quotes may be stale; never treat raw candidates as P&L.",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, json_path


def fetch_official_counts(markets: list[dict], limit: int, sleep_seconds: float) -> tuple[dict[str, int], str]:
    """Fetch official event leg counts from Gamma. Returns (counts, note)."""
    import time

    try:
        import proxy_detector

        session = proxy_detector.build_session()
    except Exception as exc:
        return {}, f"proxy unavailable: {type(exc).__name__}: {exc}"

    groups = event_groups(markets)
    ids = [
        event_id
        for event_id, group in groups.items()
        if len(group["markets"]) > 1 and group["meta"].get("enableNegRisk")
    ]
    if limit > 0:
        ids = ids[:limit]
    counts: dict[str, int] = {}
    errors = 0
    for event_id in ids:
        try:
            response = session.get(f"{GAMMA_HOST}/events/{event_id}", timeout=20)
            if response.status_code == 200:
                payload = response.json()
                event_markets = payload.get("markets") or []
                if isinstance(event_markets, list) and event_markets:
                    counts[event_id] = len(event_markets)
            else:
                errors += 1
        except Exception:
            errors += 1
        time.sleep(max(0.0, sleep_seconds))
    return counts, f"checked={len(ids)} counts={len(counts)} errors={errors}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only parity / event-basket scanner")
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    parser.add_argument("--refresh-universe", action="store_true",
                        help="fetch a fresh active-market universe from Gamma instead of the local snapshot")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--min-edge", type=float, default=0.015)
    parser.add_argument("--max-edge", type=float, default=0.10)
    parser.add_argument("--min-hours", type=float, default=2.0)
    parser.add_argument("--event-details", action="store_true", help="fetch official event leg counts from Gamma")
    parser.add_argument("--event-detail-limit", type=int, default=100)
    parser.add_argument("--books", action="store_true", help="fetch CLOB books for same-market parity")
    parser.add_argument("--book-limit", type=int, default=40)
    parser.add_argument("--min-depth-usd", type=float, default=10.0)
    parser.add_argument("--size-usd", type=float, default=10.0)
    parser.add_argument("--sleep", type=float, default=0.15)
    args = parser.parse_args()

    snapshot = Path(args.snapshot)
    markets: list[dict] = []
    discovered = ""
    snapshot_label = str(snapshot)
    universe_mode = "snapshot"
    if args.refresh_universe:
        try:
            import proxy_detector

            session = proxy_detector.build_session()
            markets, discovered = fetch_universe(session, args.sleep)
            if not markets:
                raise RuntimeError("Gamma returned an empty universe")
            snapshot_label = f"live Gamma /markets · {len(markets)} markets"
            universe_mode = "live"
            print(
                f"[parity_scan] live universe markets={len(markets)} discovered={discovered}",
                flush=True,
            )
        except Exception as exc:
            print(f"[parity_scan] live universe failed: {exc}", flush=True)
            if not snapshot.exists():
                print(
                    f"live universe failed and snapshot not found: {snapshot}",
                    file=sys.stderr,
                )
                return 2

    if not markets:
        if not snapshot.exists():
            print(f"snapshot not found: {snapshot}", file=sys.stderr)
            return 2
        markets, discovered = load_snapshot(snapshot)
        print(f"[parity_scan] snapshot markets={len(markets)} discovered={discovered}", flush=True)

    official_counts: dict[str, int] = {}
    official_note = "offline; official leg counts unavailable"
    if args.event_details:
        official_counts, official_note = fetch_official_counts(
            markets, args.event_detail_limit, args.sleep
        )
        print(f"[parity_scan] event details: {official_note}", flush=True)

    basket = scan_baskets(
        markets, args.min_edge, args.max_edge, args.min_hours, official_counts
    )
    parity: dict = {"skipped": "no --books; same-market parity needs two CLOB books"}
    if args.books:
        try:
            import proxy_detector

            session = proxy_detector.build_session()
            parity = scan_parity(
                markets,
                session,
                args.book_limit,
                args.min_edge,
                args.max_edge,
                args.min_hours,
                args.min_depth_usd,
                args.size_usd,
                args.sleep,
            )
            parity["skipped"] = None
        except Exception as exc:
            parity = {"skipped": f"{type(exc).__name__}: {exc}"}
            print(f"[parity_scan] parity skipped: {exc}", flush=True)

    report = {
        "generated_at": now_utc(),
        "snapshot": snapshot_label,
        "snapshot_at": discovered,
        "settings": {
            "universe_mode": universe_mode,
            "min_edge": args.min_edge,
            "max_edge": args.max_edge,
            "min_hours": args.min_hours,
            "event_details": args.event_details,
            "event_detail_limit": args.event_detail_limit,
            "event_detail_note": official_note,
            "books": args.books,
            "book_limit": args.book_limit,
            "min_depth_usd": args.min_depth_usd,
            "size_usd": args.size_usd,
        },
        "coverage": {
            "universe_mode": universe_mode,
            "markets": len(markets),
            "events": len(event_groups(markets)),
            "multi_leg_events": basket["multi_leg_events"],
            "eligible_events": basket["eligible_events"],
            "verified_events": basket["verified_events"],
            "basket_candidates": len(basket["candidates"]),
            "suspicious_groups": len(basket["suspicious"]),
            "basket_short_screen": len(basket["basket_short_screen"]),
            "event_details": len(official_counts),
            "parity_checked": parity.get("checked", 0),
            "parity_candidates": len(parity.get("candidates", [])),
        },
        "basket": basket,
        "parity": parity,
    }
    out_dir = Path(args.out_dir)
    md_path, json_path = write_report(report, out_dir, args.date)
    print(
        json.dumps(
            {
                "coverage": report["coverage"],
                "basket_rejects": basket["rejects"],
                "parity_rejects": parity.get("rejects", {}),
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

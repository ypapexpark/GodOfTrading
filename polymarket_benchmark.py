#!/usr/bin/env python3
from __future__ import annotations

"""
Read-only Polymarket BTC Up/Down benchmark.

This script discovers current and near-future recurring BTC Up/Down markets,
fetches public CLOB order books, and estimates spread/depth/fill quality.
It never places orders and does not require wallet/API credentials.

Examples:
  python3 polymarket_benchmark.py
  python3 polymarket_benchmark.py --recurrence 15m --ahead 8 --json
  python3 polymarket_benchmark.py --order-usd 100 500 1000
"""

import argparse
import json
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)

import requests


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DEFAULT_ORDER_USD = (100.0, 500.0, 1000.0)


@dataclass(frozen=True)
class FillEstimate:
    requested_usd: float
    filled_usd: float
    shares: float
    avg_price: float | None
    best_price: float | None
    slippage_pct_points: float | None
    unfilled_usd: float


def _get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    resp = requests.get(url, params=params, timeout=12)
    resp.raise_for_status()
    return resp.json()


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _utc_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _slot_start(now_ts: int, recurrence_seconds: int) -> int:
    return now_ts - (now_ts % recurrence_seconds)


def _slug(prefix: str, slot_ts: int) -> str:
    return f"{prefix}-{slot_ts}"


def _fetch_event(slug: str) -> dict[str, Any] | None:
    events = _get_json(f"{GAMMA_API}/events", {"slug": slug})
    if not events:
        return None
    return events[0]


def _fetch_book(token_id: str) -> dict[str, Any]:
    return _get_json(f"{CLOB_API}/book", {"token_id": token_id})


def _sorted_levels(levels: list[dict[str, Any]], reverse: bool) -> list[dict[str, float]]:
    parsed = [
        {"price": float(level["price"]), "size": float(level["size"])}
        for level in levels
        if float(level.get("price", 0) or 0) > 0 and float(level.get("size", 0) or 0) > 0
    ]
    return sorted(parsed, key=lambda x: x["price"], reverse=reverse)


def _sum_size(levels: list[dict[str, float]]) -> float:
    return sum(level["size"] for level in levels)


def _estimate_buy(levels: list[dict[str, float]], order_usd: float) -> FillEstimate:
    remaining = order_usd
    filled_usd = 0.0
    shares = 0.0
    best_price = levels[0]["price"] if levels else None

    for level in levels:
        price = level["price"]
        size = level["size"]
        if price <= 0 or size <= 0 or remaining <= 0:
            continue
        level_capacity_usd = price * size
        take_usd = min(remaining, level_capacity_usd)
        take_shares = take_usd / price
        filled_usd += take_usd
        shares += take_shares
        remaining -= take_usd
        if remaining <= 1e-9:
            break

    avg_price = filled_usd / shares if shares > 0 else None
    slippage = (
        (avg_price - best_price) if avg_price is not None and best_price is not None else None
    )
    return FillEstimate(
        requested_usd=order_usd,
        filled_usd=filled_usd,
        shares=shares,
        avg_price=avg_price,
        best_price=best_price,
        slippage_pct_points=slippage,
        unfilled_usd=max(order_usd - filled_usd, 0.0),
    )


def _analyze_book(book: dict[str, Any], order_sizes: tuple[float, ...],
                  depth_width: float) -> dict[str, Any]:
    bids = _sorted_levels(book.get("bids", []), reverse=True)
    asks = _sorted_levels(book.get("asks", []), reverse=False)
    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None
    spread = (
        round(best_ask - best_bid, 6)
        if best_bid is not None and best_ask is not None
        else None
    )
    near_bids = [
        level for level in bids
        if best_bid is not None and level["price"] >= max(best_bid - depth_width, 0)
    ]
    near_asks = [
        level for level in asks
        if best_ask is not None and level["price"] <= min(best_ask + depth_width, 1)
    ]
    fills = [_estimate_buy(asks, order_usd) for order_usd in order_sizes]

    return {
        "asset_id": book.get("asset_id"),
        "market": book.get("market"),
        "tick_size": book.get("tick_size"),
        "min_order_size": book.get("min_order_size"),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "bid_depth_near_shares": _sum_size(near_bids),
        "ask_depth_near_shares": _sum_size(near_asks),
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "fills": [fill.__dict__ for fill in fills],
    }


def discover_markets(recurrence: str, lookback: int, ahead: int,
                     order_sizes: tuple[float, ...],
                     depth_width: float) -> list[dict[str, Any]]:
    recurrence_seconds = {"5m": 300, "15m": 900}[recurrence]
    prefix = {"5m": "btc-updown-5m", "15m": "btc-updown-15m"}[recurrence]
    current = _slot_start(int(time.time()), recurrence_seconds)
    rows: list[dict[str, Any]] = []

    for offset in range(-lookback, ahead + 1):
        slot_ts = current + offset * recurrence_seconds
        slug = _slug(prefix, slot_ts)
        event = _fetch_event(slug)
        if not event:
            rows.append({
                "slug": slug,
                "slot_ts": slot_ts,
                "slot_utc": _utc_iso(slot_ts),
                "found": False,
            })
            continue

        market = (event.get("markets") or [{}])[0]
        token_ids = _parse_jsonish(market.get("clobTokenIds") or "[]")
        outcomes = _parse_jsonish(market.get("outcomes") or "[]")
        token_rows = []
        for outcome, token_id in zip(outcomes, token_ids):
            try:
                book = _fetch_book(str(token_id))
                analysis = _analyze_book(book, order_sizes, depth_width)
            except Exception as exc:
                analysis = {"asset_id": str(token_id), "error": str(exc)}
            analysis["outcome"] = outcome
            token_rows.append(analysis)

        rows.append({
            "slug": slug,
            "slot_ts": slot_ts,
            "slot_utc": _utc_iso(slot_ts),
            "found": True,
            "title": event.get("title"),
            "active": event.get("active"),
            "closed": event.get("closed"),
            "start_time": event.get("startTime"),
            "end_date": event.get("endDate"),
            "volume_24h": event.get("volume24hr"),
            "liquidity": event.get("liquidity"),
            "event_metadata": event.get("eventMetadata") or {},
            "condition_id": market.get("conditionId"),
            "accepting_orders": market.get("acceptingOrders"),
            "fees_enabled": market.get("feesEnabled"),
            "fee_schedule": market.get("feeSchedule"),
            "event_best_bid": market.get("bestBid"),
            "event_best_ask": market.get("bestAsk"),
            "event_spread": market.get("spread"),
            "tokens": token_rows,
        })

    return rows


def _fmt_price(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.4f}"
    except Exception:
        return str(value)


def _fmt_num(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):,.2f}"
    except Exception:
        return str(value)


def print_table(rows: list[dict[str, Any]], order_sizes: tuple[float, ...]) -> None:
    print("Polymarket BTC Up/Down read-only benchmark")
    print(f"Generated UTC: {datetime.now(timezone.utc).isoformat()}")
    print()
    for row in rows:
        if not row.get("found"):
            print(f"{row['slug']} | not found | slot {row['slot_utc']}")
            continue
        print(
            f"{row['slug']} | {row.get('title')} | "
            f"closed={row.get('closed')} accepting={row.get('accepting_orders')} "
            f"vol24h={_fmt_num(row.get('volume_24h'))} liq={_fmt_num(row.get('liquidity'))}"
        )
        for token in row.get("tokens", []):
            if token.get("error"):
                print(f"  {token.get('outcome'):<5} ERROR {token['error']}")
                continue
            fill_bits = []
            for fill in token.get("fills", []):
                avg = _fmt_price(fill.get("avg_price"))
                unfilled = fill.get("unfilled_usd") or 0.0
                suffix = "" if unfilled <= 1e-6 else f" unfilled=${unfilled:.2f}"
                fill_bits.append(f"${fill['requested_usd']:.0f}@{avg}{suffix}")
            print(
                f"  {str(token.get('outcome')):<5} "
                f"bid={_fmt_price(token.get('best_bid'))} "
                f"ask={_fmt_price(token.get('best_ask'))} "
                f"spr={_fmt_price(token.get('spread'))} "
                f"nearBid={_fmt_num(token.get('bid_depth_near_shares'))}sh "
                f"nearAsk={_fmt_num(token.get('ask_depth_near_shares'))}sh "
                f"fills: {', '.join(fill_bits)}"
            )
        print()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recurrence", choices=("5m", "15m"), default="5m")
    parser.add_argument("--lookback", type=int, default=2)
    parser.add_argument("--ahead", type=int, default=4)
    parser.add_argument("--depth-width", type=float, default=0.05)
    parser.add_argument("--order-usd", nargs="+", type=float, default=list(DEFAULT_ORDER_USD))
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    order_sizes = tuple(float(x) for x in args.order_usd)
    rows = discover_markets(
        recurrence=args.recurrence,
        lookback=max(args.lookback, 0),
        ahead=max(args.ahead, 0),
        order_sizes=order_sizes,
        depth_width=max(args.depth_width, 0.0),
    )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print_table(rows, order_sizes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
